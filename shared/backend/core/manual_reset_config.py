# manual_reset_config.py — 手动重置到期列表“手动配置”兜底（方案 C，2026-09-07）
"""当 API 实时到期列表不可用时，可由用户确认日期后写本地配置，设备显示该列表。

文件：<backend>/state/manual_reset_config.json
  {
    "expiry_list": [<unix秒, 升序, 仅未过期>],
    "credits_available_at_confirm": <int>,   // 确认时的可用次数
    "confirmed_at": <unix秒>,                 // 确认时间
    "note": "手动配置（来源 manual）"
  }
规则：
  - 仅“用户确认日期后”写入；旧快照/未经确认的历史日期不得直接发布。
  - API 实时列表存在时优先 API；manual 仅作兜底，来源标记 manual（≠ api）。
  - 次数与列表不一致：调用方标记待核对；本模块只保存配置与确认信息。
  - 与 api 自动采集并存：config 工具 set 时把 api 视为不可用前提由调用方保证；
    本模块不自动覆盖 api 结果。
"""
from __future__ import annotations
from . import state_store

import time
from pathlib import Path

_FILE = state_store.state_path("manual_reset_config.json")


def configure_file(path) -> None:
    global _FILE
    _FILE = Path(path)


def load_manual_reset() -> dict | None:
    """读取手动配置；无文件/损坏 → None（不当作空列表，不冒充 0 次）。"""
    d, error = state_store.read_json(_FILE)
    if error or not isinstance(d, dict):
        return None
    exp = d.get("expiry_list")
    return d if isinstance(exp, list) else None


def save_manual_reset(expiry_list, credits_available_at_confirm, note: str = "") -> dict:
    """写入/更新手动配置；expiry_list 必须为升序 Unix 秒（只留未过期项）。"""
    now = int(time.time())
    exp = sorted({int(x) for x in expiry_list if isinstance(x, (int, float)) and int(x) > now})
    d = {
        "expiry_list": exp,
        "credits_available_at_confirm": int(credits_available_at_confirm or 0),
        "confirmed_at": now,
        "note": note or "手动配置（来源 manual）",
    }
    state_store.write_json(_FILE, d)
    return d


def clear_manual_reset() -> bool:
    try:
        return state_store.remove_json(_FILE)
    except (OSError, state_store.StateStoreError):
        return False
