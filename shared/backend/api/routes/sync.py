"""双后端同步端点（接口契约 §3.3）。

GET /api/sync[?since=<unix|iso>]   Header: Authorization: Bearer <SYNC_TOKEN>

v1 语义（个人规模）：
  - 拉取式双向：两端各自定时 GET 对端 → 本地合并（updated_at 新者胜，见 inksight-tools/sync_backends.py）
  - 默认返回全部行（数据量小）；since 参数预留增量
  - 仅同步设备可工作所需的最小表集；users 表不同步（副本后端用环境变量 Key 提供内容，个人 LLM Key/配额在副本上降级）
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from core.db import get_main_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])

# 表名 -> 时间戳列（用于新者胜合并；列不存在时降级为全量+本地优先）
SYNC_TABLES: dict[str, str] = {
    "configs": "created_at",
    "device_state": "updated_at",
    "user_devices": "bound_at",
    "device_claim_tokens": "created_at",
    "codex_usage": "updated_at",
    "ai_usage_state": "updated_at",
}


def _extract_token(x_device_token: Optional[str], authorization: Optional[str]) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_device_token and x_device_token.strip():
        return x_device_token.strip()
    return ""


async def _column_names(con, table: str) -> list[str]:
    cur = await con.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    return [r[1] for r in rows]


async def _rows_since(con, table: str, ts_col: str, since) -> list[dict]:
    cols = await _column_names(con, table)
    if not cols:
        return []
    if ts_col in cols and since:
        cur = await con.execute(
            f"SELECT * FROM {table} WHERE CAST({ts_col} AS TEXT) > ?",
            (str(since),),
        )
    else:
        cur = await con.execute(f"SELECT * FROM {table}")
    rows = await cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


@router.get("/sync")
async def get_sync(
    since: Optional[str] = None,
    authorization: Optional[str] = Header(default=None),
    x_device_token: Optional[str] = Header(default=None),
):
    expected = os.getenv("SYNC_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="sync disabled (SYNC_TOKEN not set)")
    token = _extract_token(x_device_token, authorization)
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    db = await get_main_db()
    out: dict[str, list[dict]] = {}
    timestamps: dict[str, str] = {}
    for table, ts_col in SYNC_TABLES.items():
        try:
            rows = await _rows_since(db, table, ts_col, since)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SYNC] table %s read failed: %s", table, e)
            continue
        out[table] = rows
        if rows:
            timestamps[table] = ts_col
    return {"tables": out, "timestamps": timestamps, "generated_at": None}
