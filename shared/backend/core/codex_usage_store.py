"""codex-usage 记录存储：按 (device_mac, source) 存最新一条。

读取优先级（接口契约 §2.1，Mac 权威）：
  1. source=mac 且新鲜（< stale_seconds）→ 用 Mac 数据；
  2. 否则用最新一条其它源；
  3. 全部过期 → 返回最新一条并标记 stale=True。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .db import get_main_db

logger = logging.getLogger(__name__)

SOURCES = ("mac", "windows")


async def _ensure_schema(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS codex_usage (
               device_mac TEXT NOT NULL,
               source     TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               ts         REAL NOT NULL,
               updated_at REAL NOT NULL,
               PRIMARY KEY (device_mac, source))"""
    )
    await db.commit()


async def set_codex_usage(mac: str, source: str, payload: dict) -> None:
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    db = await get_main_db()
    await _ensure_schema(db)
    now = time.time()
    ts = float(payload.get("ts") or now)
    await db.execute(
        """INSERT INTO codex_usage (device_mac, source, payload_json, ts, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(device_mac, source) DO UPDATE SET
               payload_json = excluded.payload_json,
               ts = excluded.ts,
               updated_at = excluded.updated_at""",
        (mac.upper(), source, json.dumps(payload, ensure_ascii=False), ts, now),
    )
    await db.commit()


async def get_codex_usage(mac: str, stale_seconds: int = 7200) -> Optional[dict]:
    if not mac:
        return None
    db = await get_main_db()
    await _ensure_schema(db)
    cur = await db.execute(
        "SELECT source, payload_json, ts, updated_at FROM codex_usage "
        "WHERE device_mac = ? ORDER BY updated_at DESC",
        (mac.upper(),),
    )
    rows = await cur.fetchall()
    if not rows:
        return None

    now = time.time()
    mac_fresh: Optional[dict] = None
    fallback_fresh: Optional[dict] = None
    for source, payload_json, ts, updated_at in rows:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        fresh = (now - float(ts or updated_at)) < stale_seconds
        if source == "mac" and fresh and mac_fresh is None:
            mac_fresh = payload
        elif fresh and fallback_fresh is None:
            fallback_fresh = payload

    selected = mac_fresh or fallback_fresh
    if selected is not None:
        return selected
    # 全部过期：取最新一条并标记 stale
    _, payload_json, _, _ = rows[0]
    try:
        selected = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(selected, dict):
        selected = dict(selected)
        selected["stale"] = True
    return selected


async def get_codex_usage_any(stale_seconds: int = 7200) -> Optional[dict]:
    """无设备上下文（云端发布端拉内容、不绑定设备）时读取：全库最新一条。

    沿用"Mac 权威"语义：优先 source=mac 且新鲜的数据；否则任一新来源；
    全过期则返回最新一条并标记 stale=True。云端多设备共用同一条订阅数据。
    """
    db = await get_main_db()
    await _ensure_schema(db)
    cur = await db.execute(
        "SELECT source, payload_json, ts, updated_at FROM codex_usage "
        "ORDER BY (source = 'mac') DESC, updated_at DESC"
    )
    rows = await cur.fetchall()
    if not rows:
        return None

    now = time.time()
    mac_fresh: Optional[dict] = None
    fallback_fresh: Optional[dict] = None
    latest: Optional[dict] = None
    for source, payload_json, ts, updated_at in rows:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload = dict(payload)
        payload.setdefault("source", source)
        if latest is None:
            latest = payload
        fresh = (now - float(ts or updated_at)) < stale_seconds
        if source == "mac" and fresh and mac_fresh is None:
            mac_fresh = payload
        elif fresh and fallback_fresh is None:
            fallback_fresh = payload

    selected = mac_fresh or fallback_fresh or latest
    if selected is None:
        return None
    if selected is not mac_fresh and selected is not fallback_fresh:
        selected = dict(selected)
        selected["stale"] = True
    return selected
