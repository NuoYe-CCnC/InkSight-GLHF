"""reliable_sources.py — 可靠采集层（第一阶段）

职责：
  1) Codex：从任意结构(primary/secondary/legacy windows)显式挑 7D 窗口
     (duration_minutes == 10080)；缺失 → unknown（不得用 5H/首桶冒充）。
     记录/读取走 data_cache 组 ai.codex。
  2) DeepSeek：按 currency 匹配 CNY/USD 独立缓存（不依赖数组顺序）；
     合法 0 是有效值；对"突变为 0"做一次即时复核，复核仍 0 则接受。
  3) 会员配置：读取 backend/data/member_config.json（用户确认值，
     valid_until_date 仅日期、precision=date，不补 00:00/23:59）。

全部为文本/数值证据；不输出任何凭据。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import data_cache as dc

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
MEMBER_CONFIG_FILE = BACKEND_ROOT / "data" / "member_config.json"
SEVEN_DAY_MINUTES = 10080

# ── Codex：7D 窗口选择 ──────────────────────────────────────
def pick_seven_day(normalized: dict) -> Optional[dict]:
    """从 codex_quota_probe.normalize() 产物中选 7D 窗口。

    返回 {label,duration_minutes,used_percent,resets_at} 或 None。
    used_percent 缺失/非法视为无效。7D 缺失返回 None（由调用方置 unknown）。
    """
    if not isinstance(normalized, dict):
        return None
    windows = normalized.get("windows") or []
    if not isinstance(windows, list):
        return None
    for w in windows:
        if not isinstance(w, dict):
            continue
        if int(w.get("duration_minutes") or 0) != SEVEN_DAY_MINUTES:
            continue
        up = w.get("used_percent")
        if up is None:
            continue
        return {
            "label": w.get("label") or "7D",
            "duration_minutes": SEVEN_DAY_MINUTES,
            "used_percent": float(up),
            "resets_at": w.get("resets_at"),
        }
    return None


def ai_key_snapshot(codex: dict, ds: dict) -> dict:
    """AI 关键版本种子：只含用量/余额/重置/会员，不含采集时间等易变字段。"""
    return {
        "codex_7d_used": codex.get("used_percent"),
        "codex_resets_at": codex.get("resets_at"),
        "codex_reset_credits": codex.get("reset_credits_available"),
        "ds_cny": ds.get("CNY"),
        "ds_usd": ds.get("USD"),
        "member": member_config(),
        "deepseek_today": _deepseek_today(),
    }


# ── 会员配置 ────────────────────────────────────────────────
_member_cache: Optional[dict] = None


def member_config() -> dict:
    global _member_cache
    if _member_cache is None:
        try:
            from .manual_settings import member as _manual_member
            _member_cache = _manual_member()
            if not _member_cache:
                _member_cache = json.loads(MEMBER_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("member_config 读取失败: %s", e)
            _member_cache = {}
    return dict(_member_cache or {})


def reload_member_config() -> dict:
    global _member_cache
    _member_cache = None
    return member_config()


# ── DeepSeek：by-currency 独立采集与缓存 ─────────────────────
def fetch_deepseek_balances(timeout_sec: float = 20) -> Optional[dict]:
    """GET https://api.deepseek.com/user/balance，按 currency 提取 CNY/USD。

    返回 {"CNY": float|None, "USD": float|None, "is_available": bool}；
    网络/认证失败返回 None。不抛异常给调用方。
    """
    env = {}
    env_file = BACKEND_ROOT / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    except OSError:
        return None
    try:
        from .manual_settings import secret as _manual_secret
        key = _manual_secret("deepseek_api_key") or env.get("DEEPSEEK_API_KEY", "")
    except Exception:  # noqa: BLE001
        key = env.get("DEEPSEEK_API_KEY", "")
    if not key:
        logger.warning("deepseek .env 缺少 DEEPSEEK_API_KEY")
        return None
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("deepseek balance 请求失败: %s", e)
        return None
    out = {"CNY": None, "USD": None, "is_available": bool(data.get("is_available"))}
    for bi in data.get("balance_infos") or []:
        cur = str(bi.get("currency") or "").upper()
        if cur in ("CNY", "USD"):
            try:
                out[cur] = float(str(bi.get("total_balance") or "0").replace(",", ""))
            except (TypeError, ValueError):
                pass
    return out


def cache_deepseek_result(data: dict) -> dict:
    """内容层/调用方已取到 /user/balance 响应时，按其 balance_infos 落库（不重复请求）。

    返回 {"CNY": float|None, "USD": float|None}；合法 0 直接接受（已在线复核过的现场值）。
    """
    out = {"CNY": None, "USD": None}
    for bi in (data.get("balance_infos") or []):
        if not isinstance(bi, dict):
            continue
        cur = str(bi.get("currency") or "").upper()
        if cur not in ("CNY", "USD"):
            continue
        try:
            out[cur] = float(str(bi.get("total_balance") or "0").replace(",", ""))
        except (TypeError, ValueError):
            out[cur] = None
    for cur, v in out.items():
        if v is not None:
            dc.record_success(f"ai.ds.{cur.lower()}", v,
                              meta={"currency": cur, "unit": "CNY" if cur == "CNY" else "USD"})
        else:
            dc.record_failure(f"ai.ds.{cur.lower()}", note="no balance entry")
    if out.get("CNY") is not None:
        try:
            from .deepseek_token_tracker import observe_balance
            observe_balance(out["CNY"])
        except Exception:  # noqa: BLE001
            logger.debug("deepseek token balance observation skipped")
    return out


def _store_ds_group(currency: str, value: Optional[float]) -> None:
    group = f"ai.ds.{currency.lower()}"
    if value is None:
        dc.record_failure(group, note="no balance entry for currency")
        return
    # 突变为 0 复核：一次即时重查仍 0 → 接受为合法 0
    prev = (dc.get_group(group) or {}).get("value")
    if value == 0.0 and isinstance(prev, (int, float)) and prev > 0:
        again = fetch_deepseek_balances()
        if again and again.get(currency) not in (None, 0.0):
            value = again[currency]  # 复核非 0：采用复核值
    dc.record_success(group, value, meta={"currency": currency, "unit": "CNY" if currency == "CNY" else "USD"})
    if currency == "CNY":
        try:
            from .deepseek_token_tracker import observe_balance
            observe_balance(value)
        except Exception:  # noqa: BLE001
            logger.debug("deepseek token balance observation skipped")


def _deepseek_today() -> dict:
    try:
        from .deepseek_token_tracker import snapshot
        return snapshot()
    except Exception:  # noqa: BLE001
        return {"tokens": 0, "complete": False, "source": "unavailable"}


def refresh_deepseek() -> dict:
    """一次完整 DeepSeek 刷新：失败保留缓存；返回 {ok, balances, statuses}。"""
    fetched = fetch_deepseek_balances()
    if fetched is None:
        for cur in ("CNY", "USD"):
            dc.record_failure(f"ai.ds.{cur.lower()}", note="fetch failed")
        return {"ok": False, "balances": _ds_values(), "statuses": _ds_statuses()}
    for cur in ("CNY", "USD"):
        _store_ds_group(cur, fetched.get(cur))
    return {"ok": True, "balances": _ds_values(), "statuses": _ds_statuses()}


def _ds_values() -> dict:
    out = {}
    for cur in ("CNY", "USD"):
        g = dc.get_group(f"ai.ds.{cur.lower()}") or {}
        v = g.get("value")
        out[cur] = v if v is not None else None
    return out


def _ds_statuses() -> dict:
    return {cur: dc.group_status(f"ai.ds.{cur.lower()}") for cur in ("CNY", "USD")}


def ds_values_cached() -> dict:
    """内容层读取：只读缓存，不触发网络。"""
    return _ds_values()
