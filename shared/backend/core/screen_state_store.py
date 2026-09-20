"""屏幕级状态存储：记录每个模式最近一次交付的逐模块 payload_id。

用途：delta 交付（接口契约 §1.3 / §1.5.2）——设备轮询时服务端对比"本次生成的模块
payload_id vs 上次交付"，只下发变化的模块（刷新隔离）；无变化返回 noop。
"""
from __future__ import annotations

import json
import logging
import time

from .db import get_main_db

logger = logging.getLogger(__name__)


async def _ensure_schema(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS screen_delta_state (
               mac TEXT NOT NULL,
               mode TEXT NOT NULL,
               ids_json TEXT NOT NULL,
               updated_at REAL NOT NULL,
               PRIMARY KEY (mac, mode))"""
    )
    await db.commit()


async def set_module_ids(mac: str, mode: str, module_ids: dict) -> None:
    db = await get_main_db()
    await _ensure_schema(db)
    await db.execute(
        """INSERT INTO screen_delta_state (mac, mode, ids_json, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(mac, mode) DO UPDATE SET
               ids_json = excluded.ids_json, updated_at = excluded.updated_at""",
        (mac.upper(), mode.upper(), json.dumps(module_ids, ensure_ascii=False), time.time()),
    )
    await db.commit()


async def get_module_ids(mac: str, mode: str) -> dict:
    if not mac:
        return {}
    db = await get_main_db()
    await _ensure_schema(db)
    cur = await db.execute(
        "SELECT ids_json FROM screen_delta_state WHERE mac = ? AND mode = ?",
        (mac.upper(), mode.upper()),
    )
    row = await cur.fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0]) or {}
    except (json.JSONDecodeError, TypeError):
        return {}
