"""AI_USAGE 变化触发刷新 watcher（服务端）。

规则（接口契约 §2.1 补充）：
  - Codex 额度：生效 payload 中任一窗口 used_percent 变化 ≥ codex_refresh_delta_pct（默认 1%）→ 置 pending 刷新
  - DeepSeek 余额：total_balance 变化 ≥ deepseek_refresh_delta_cny（默认 1 元）→ 置 pending 刷新
  - 仅对当前模式为 AI_USAGE 的设备生效（读 device_state.last_persona）
  - 基线首次建立时也置 pending（保证设备尽快显示首帧）

设备端配套（P1 固件）：
  - fetchBMP/fetchStructured 收集 X-Pending-Refresh 头 → 置位时立即刷新
  - 无变化轮询跳过屏幕刷新；每 30 分钟健康全刷（清残影）

注意：DeepSeek key 解析 v1 = config.user_api_key → 环境变量 DEEPSEEK_API_KEY；
用户级加密 key（llmApiKey）的解密接入列为待办。
"""
from __future__ import annotations

import json
import logging
import os
import time

from .db import get_main_db
from .config_store import get_active_config, get_device_state, set_pending_refresh

logger = logging.getLogger(__name__)

DEFAULT_CODEX_DELTA_PCT = 1.0
DEFAULT_DEEPSEEK_DELTA_CNY = 1.0
WATCHER_INTERVAL_SECONDS = 60
_BALANCE_CACHE_TTL = 55
_balance_cache: dict[str, tuple[float, dict]] = {}  # key -> (fetched_at, payload)


async def _ensure_state_table(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS ai_usage_state (
               mac TEXT PRIMARY KEY,
               last_deepseek_balance REAL,
               last_codex_used_pct REAL,
               updated_at REAL NOT NULL)"""
    )
    await db.commit()


async def get_baseline(mac: str) -> dict:
    db = await get_main_db()
    await _ensure_state_table(db)
    cur = await db.execute(
        "SELECT last_deepseek_balance, last_codex_used_pct FROM ai_usage_state WHERE mac = ?",
        (mac.upper(),),
    )
    row = await cur.fetchone()
    if not row:
        return {}
    return {"deepseek": row[0], "codex": row[1]}


async def set_baseline(mac: str, deepseek: float | None = None, codex: float | None = None) -> None:
    db = await get_main_db()
    await _ensure_state_table(db)
    await db.execute(
        """INSERT INTO ai_usage_state (mac, last_deepseek_balance, last_codex_used_pct, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(mac) DO UPDATE SET
               last_deepseek_balance = COALESCE(?, last_deepseek_balance),
               last_codex_used_pct = COALESCE(?, last_codex_used_pct),
               updated_at = ?""",
        (mac.upper(), deepseek, codex, time.time(), deepseek, codex, time.time()),
    )
    await db.commit()


async def fetch_deepseek_balance(api_key: str) -> dict | None:
    """拉取 DeepSeek 余额（带 5 分钟内存缓存）。"""
    if not api_key:
        return None
    now = time.time()
    cached = _balance_cache.get(api_key)
    if cached and now - cached[0] < _BALANCE_CACHE_TTL:
        return cached[1]
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            data = r.json()
        bi = {}
        if isinstance(data, dict):
            infos = data.get("balance_infos") or []
            # 账户可能同时有 USD 与 CNY 两笔余额：优先取 CNY（充值那笔），
            # 否则退到第一条（老 bug：只取 infos[0]，显示成 USD/0.00）。
            entries = [it for it in infos if isinstance(it, dict)]
            for it in entries:
                cur = str(it.get("currency") or "").upper()
                if cur in ("CNY", "RMB"):
                    bi = it
                    break
            else:
                bi = entries[0] if entries else {}
            if len(entries) > 1:
                logger.info("[WATCHER] DeepSeek multi-currency balances: %s",
                            [(str(e.get('currency')), e.get('total_balance')) for e in entries])
        out = {
            "is_available": bool(data.get("is_available")) if isinstance(data, dict) else False,
            "total_balance": bi.get("total_balance", "--"),
            "currency": bi.get("currency", "CNY"),
        }
        _balance_cache[api_key] = (now, out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[WATCHER] DeepSeek balance fetch failed: %s", e)
        return None


async def _resolve_deepseek_key(mac: str, config: dict | None) -> str:
    """解析设备的 DeepSeek key：config.user_api_key → 设备主人个人 key（已解密）→ 环境变量。"""
    if config:
        key = (config.get("user_api_key") or "").strip()
        if key:
            return key
    try:
        from .config_store import get_device_owner, get_user_llm_config

        owner = await get_device_owner(mac)
        if owner and owner.get("user_id"):
            ucfg = await get_user_llm_config(int(owner["user_id"]))
            if ucfg and (ucfg.get("provider") or "").lower() == "deepseek":
                k = (ucfg.get("api_key") or "").strip()
                if k:
                    return k
    except Exception as e:  # noqa: BLE001
        logger.debug("[WATCHER] owner key resolution failed: %s", e)
    try:
        from .manual_settings import secret
        key = secret("deepseek_api_key")
        if key:
            return key
    except Exception:  # noqa: BLE001
        pass
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def _effective_codex_pct(payload: dict | None) -> float | None:
    """生效 codex payload 中所有窗口 used_percent 的最大值（最接近耗尽的窗口）。"""
    if not payload or not isinstance(payload, dict):
        return None
    vals = []
    for w in payload.get("windows") or []:
        u = w.get("used_percent")
        if isinstance(u, (int, float)):
            vals.append(float(u))
    return max(vals) if vals else None


def _parse_balance(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


async def check_device(mac: str,
                       deepseek_balance_override: dict | None = None,
                       codex_payload_override: dict | None = None) -> bool:
    """检查一台设备是否达到变化阈值；达到则置 pending 刷新。返回是否触发。"""
    mac = mac.strip().upper()
    try:
        config = await get_active_config(mac)
    except Exception:  # noqa: BLE001
        config = None
    if not isinstance(config, dict):
        return False

    ms = config.get("mode_settings") or {}
    if not isinstance(ms, dict):
        ms = {}
    delta_cny = float(ms.get("deepseek_refresh_delta_cny", DEFAULT_DEEPSEEK_DELTA_CNY))
    delta_pct = float(ms.get("codex_refresh_delta_pct", DEFAULT_CODEX_DELTA_PCT))

    baseline = await get_baseline(mac)
    triggered = False
    new_deepseek: float | None = None
    new_codex: float | None = None

    # ── DeepSeek ─────────────────────────────────────────────
    ds_changed = False
    if deepseek_balance_override is not None:
        bal = deepseek_balance_override
    else:
        bal = await fetch_deepseek_balance(await _resolve_deepseek_key(mac, config))
    if bal:
        parsed = _parse_balance(bal.get("total_balance"))
        if parsed is not None:
            last = baseline.get("deepseek")
            # DeepSeek 接口偶发返回 0.00（观测：13:57/14:05 两次瞬时空值又跳回），
            # 上次有效值 >0 时把 0.00 视为瞬时空值，保留上次值，避免误导显示。
            if parsed <= 0.0 and last is not None and float(last) > 0.0:
                logger.warning(
                    "[WATCHER] DeepSeek balance=0.00 treated as transient (last=%.2f), keep last",
                    float(last))
                parsed = float(last)
            new_deepseek = parsed
            ds_changed = last is None or abs(parsed - float(last)) >= delta_cny

    # ── Codex ────────────────────────────────────────────────
    cx_changed = False
    if codex_payload_override is not None:
        payload = codex_payload_override
    else:
        try:
            from .codex_usage_store import get_codex_usage

            payload = await get_codex_usage(mac, stale_seconds=7200)
        except Exception:  # noqa: BLE001
            payload = None
    pct = _effective_codex_pct(payload)
    if pct is not None:
        new_codex = pct
        last = baseline.get("codex")
        cx_changed = last is None or abs(pct - float(last)) >= delta_pct

    triggered = ds_changed or cx_changed
    # 只更新跨过阈值（或首次）的指标基线，避免小变化反复"重置"基线导致永不触发
    if new_deepseek is not None or new_codex is not None:
        await set_baseline(mac,
                           deepseek=new_deepseek if ds_changed else None,
                           codex=new_codex if cx_changed else None)

    if triggered:
        try:
            await set_pending_refresh(mac, True)
            logger.info("[WATCHER] %s change-triggered refresh (delta_cny=%.1f delta_pct=%.1f)",
                        mac, delta_cny, delta_pct)
        except Exception as e:  # noqa: BLE001
            logger.warning("[WATCHER] set_pending_refresh failed for %s: %s", mac, e)
    return triggered


async def _scan_all_devices() -> None:
    """扫描所有 last_persona==AI_USAGE 的设备。"""
    db = await get_main_db()
    try:
        cur = await db.execute(
            "SELECT mac FROM device_state WHERE UPPER(COALESCE(last_persona,'')) = 'AI_USAGE'"
        )
        rows = await cur.fetchall()
    except Exception as e:  # noqa: BLE001
        logger.warning("[WATCHER] scan query failed: %s", e)
        return
    for (mac,) in rows:
        try:
            await check_device(mac)
        except Exception as e:  # noqa: BLE001
            logger.warning("[WATCHER] check_device(%s) failed: %s", mac, e)


# ── APScheduler 集成 ─────────────────────────────────────────
_scheduler = None


def _rebuild_feed_if(ok: bool) -> None:
    if not ok:
        return
    try:
        from .feed_document import build_feed_document
        feed = build_feed_document(write_file=True)
        logger.info("[RELIABLE] unified_feed rebuilt versions=%s", feed.get("versions"))
    except Exception as e:  # noqa: BLE001
        logger.warning("[RELIABLE] feed rebuild error: %s", e)


def start_watcher(interval_seconds: int = WATCHER_INTERVAL_SECONDS):
    """在 FastAPI lifespan 中调用。返回 scheduler（用于关闭）。"""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("[WATCHER] apscheduler not installed, watcher disabled")
        return None
    if _scheduler is not None:
        return _scheduler
    import asyncio
    from datetime import datetime as _datetime

    def datetime_now():
        return _datetime.now()

    def _scan_job():
        # 后台线程调度：asyncio.run 自带事件循环（原 AsyncIOScheduler 在无 loop 线程报错）
        try:
            asyncio.run(_scan_all_devices())
        except Exception as e:  # noqa: BLE001
            logger.warning("[WATCHER] scan job error: %s", e)

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_scan_job, "interval",
                       seconds=interval_seconds, id="ai_usage_watcher", max_instances=1,
                       coalesce=True, misfire_grace_time=120)

    # 第一阶段可靠层周期任务：
    #   reliable_deepseek：by-currency 独立持久缓存（含突变为 0 复核），与设备无关
    #   reliable_news：三分类资讯（综合/科技AI/财经）15 分钟刷新（失败保留旧内容）
    def _reliable_deepseek_job():
        try:
            from .reliable_sources import refresh_deepseek
            r = refresh_deepseek()
            logger.info("[RELIABLE] deepseek refreshed ok=%s cny=%s usd=%s statuses=%s",
                        r.get("ok"),
                        r.get("balances", {}).get("CNY"),
                        r.get("balances", {}).get("USD"),
                        r.get("statuses"))
            _rebuild_feed_if(r.get("ok"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RELIABLE] deepseek job error: %s", e)

    def _reliable_news_job():
        try:
            from .news_feed import refresh_all
            res = refresh_all()
            logger.info("[RELIABLE] news refreshed: %s",
                        {k: ("ok" if v.get("ok") else v.get("error") or "dup")
                         for k, v in res.items()})
            _rebuild_feed_if(any(v.get("ok") for v in res.values()))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RELIABLE] news job error: %s", e)

    _scheduler.add_job(_reliable_deepseek_job, "interval", seconds=interval_seconds,
                       id="reliable_deepseek", max_instances=1, coalesce=True,
                       misfire_grace_time=120, next_run_time=datetime_now())
    _scheduler.add_job(_reliable_news_job, "interval", seconds=900,
                       id="reliable_news", max_instances=1, coalesce=True,
                       misfire_grace_time=120, next_run_time=datetime_now())

    def _reliable_feed_job():
        try:
            from .feed_document import build_feed_document
            feed = build_feed_document(write_file=True)
            logger.info("[RELIABLE] unified_feed written versions=%s",
                        feed.get("versions"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RELIABLE] feed job error: %s", e)

    _scheduler.add_job(_reliable_feed_job, "interval", seconds=interval_seconds,
                       id="reliable_feed", max_instances=1, coalesce=True,
                       misfire_grace_time=120, next_run_time=datetime_now())
    def _reliable_gold_job():
        try:
            from .gold_feed import refresh as gold_refresh
            r = gold_refresh()
            logger.info("[RELIABLE] XAUS refreshed ok=%s slot=%s coalesced=%s status=%s",
                        r.get("ok"), r.get("window"), r.get("coalesced", False),
                        r.get("status"))
            _rebuild_feed_if(r.get("ok"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RELIABLE] gold job error: %s", e)

    from .gold_feed import cached as _gold_cached
    # APScheduler treats an explicit ``next_run_time=None`` as a paused job.
    # Omit the option when cache already exists so the cron trigger advances to
    # the next natural :00/:30 slot; only a cold start needs an immediate run.
    _gold_start = ({"next_run_time": datetime_now()}
                   if _gold_cached() is None else {})
    _scheduler.add_job(_reliable_gold_job, "cron", minute="0,30", second=0,
                       id="reliable_gold", max_instances=1, coalesce=True,
                       timezone="Asia/Shanghai", misfire_grace_time=120,
                       **_gold_start)

    def _news_brief_job():
        try:
            from .news_schedule import tick as _brief_tick
            r = _brief_tick()
            logger.info("[BRIEF] tick done_slots=%s latest=%s degraded_calendar=%s",
                        r.get("done_slots"), r.get("latest"), r.get("degraded_calendar"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[BRIEF] tick error: %s", e)

    _scheduler.add_job(_news_brief_job, "interval", seconds=60,
                       id="news_brief", max_instances=1, coalesce=True,
                       misfire_grace_time=120)

    def _host_start_recovery_job():
        try:
            from .host_recovery import recover
            r = recover("service-start")
            logger.info("[RECOVERY] service start deduped=%s gold=%s news=%s",
                        r.get("deduped"), (r.get("gold") or {}).get("action"),
                        (r.get("news") or {}).get("done"))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RECOVERY] service start failed: %s", type(e).__name__)

    _scheduler.add_job(_host_start_recovery_job, "date", run_date=datetime_now(),
                       id="host_start_recovery", max_instances=1,
                       misfire_grace_time=300)
    _scheduler.start()
    logger.info("[WATCHER] started (interval=%ss) + reliable jobs + news_brief", interval_seconds)
    return _scheduler


def stop_watcher() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        _scheduler = None
