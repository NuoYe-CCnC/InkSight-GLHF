"""quota_history_store — DeepSeek/Codex 额度历史快照（变化即记 + 至少 10 分钟抽样）。

- 发布端（/api/admin/structured，mac 为空）每次生成 AI_USAGE 时调用 record_if_changed：
  数值与上一条不同 → 记一条（变化事件）；相同 → 若距上次已 ≥10 分钟再记（防呆抽样）。
- 设备端按 mac 渲染不记录（避免每设备重复刷历史）。
- 保留 90 天，插入时顺带清理。
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from .db import get_main_db

logger = logging.getLogger(__name__)

_PRUNE_DAYS = 90
_SAMPLE_SECONDS = 600


async def _ensure_schema(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS quota_history (
               ts        INTEGER NOT NULL,
               deepseek  TEXT,
               codex_pct REAL,
               codex_label TEXT,
               plan      TEXT
           )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quota_history_ts ON quota_history(ts)")
    await db.commit()


async def record_if_changed(deepseek: Optional[str], codex_pct: Optional[float],
                            codex_label: Optional[str], plan: Optional[str]) -> None:
    """值变化或距上次 ≥10 分钟才写入；deepseek 为占位符('--'/空)时跳过记录。"""
    if deepseek in (None, "", "--"):
        deepseek = None
    if deepseek is None and codex_pct is None:
        return
    db = await get_main_db()
    await _ensure_schema(db)
    now = int(time.time())
    row = (await (await db.execute(
        "SELECT ts, deepseek, codex_pct FROM quota_history ORDER BY ts DESC LIMIT 1"
    )).fetchall()) or None
    if row:
        last_ts, last_ds, last_cx = row[0]
        same = (last_ds == deepseek) and (
            (last_cx is None and codex_pct is None) or
            (last_cx is not None and codex_pct is not None and abs(last_cx - codex_pct) < 0.05)
        )
        if same and (now - last_ts) < _SAMPLE_SECONDS:
            return  # 无变化且未到抽样点
    await db.execute(
        "INSERT INTO quota_history (ts, deepseek, codex_pct, codex_label, plan) VALUES (?, ?, ?, ?, ?)",
        (now, deepseek, codex_pct, codex_label, plan),
    )
    # 顺带清理过期行
    try:
        await db.execute("DELETE FROM quota_history WHERE ts < ?", (now - _PRUNE_DAYS * 86400,))
    except Exception:  # noqa: BLE001
        pass
    await db.commit()


async def get_history(days: int = 30) -> List[dict]:
    db = await get_main_db()
    await _ensure_schema(db)
    since = int(time.time()) - days * 86400
    cur = await db.execute(
        "SELECT ts, deepseek, codex_pct, codex_label, plan FROM quota_history "
        "WHERE ts >= ? ORDER BY ts ASC",
        (since,),
    )
    rows = await cur.fetchall()
    return [
        {"ts": ts, "deepseek": ds, "codex_pct": cx, "codex_label": lbl, "plan": pl}
        for ts, ds, cx, lbl, pl in rows
    ]
