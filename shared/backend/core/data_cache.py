"""data_cache.py — 发布端可靠数据缓存与状态（第一阶段新增，独立无依赖）

每组数据维护：
    value          最后一次成功值（失败不清零；缺失 ≠ 0）
    last_success   最后成功采集时间（epoch，失败不更新）
    last_attempt   最后尝试时间（epoch）
    status          fresh（成功且新鲜） / stale（有值但过期或最近失败） / unknown（从未成功）
    meta           可选附注（unit/source/note 等，内容层透传）

落盘：backend/state/data_cache.json，原子替换并保留上一代备份；
后台重启不丢失。进程内与跨进程写入均串行化。
"""
from __future__ import annotations
from . import state_store

import json
import logging
import threading
import time
from . import state_store
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_FILE = state_store.state_path("data_cache.json")

STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

_lock = threading.Lock()
_file_path: Path = DEFAULT_CACHE_FILE


def configure_cache_file(path) -> None:
    """测试或部署时改缓存文件位置（相对路径按 backend 根解析）。"""
    global _file_path
    p = Path(path)
    _file_path = p if p.is_absolute() else (_BACKEND_ROOT / p)


def _default() -> dict:
    return {
        "schema": 1,
        "groups": {},   # group -> {value,last_success,last_attempt,status,meta}
        "versions": {},  # key(ai_key/news/gold) -> str/number
        "saved_at": None,
    }


def _load_unlocked() -> dict:
    raw, error = state_store.read_json(_file_path)
    if isinstance(raw, dict) and raw.get("schema") == 1 and not error:
        raw.setdefault("groups", {})
        raw.setdefault("versions", {})
        return raw
    if error and error != "missing":
        logger.warning("data_cache 读取失败(%s)，只读按空缓存；写入保持暂停", error)
    d = _default()
    if error:
        d["_state_error"] = error
    return d


def _save_unlocked(data: dict) -> None:
    data = dict(data)
    data.pop("_state_error", None)
    data["saved_at"] = time.time()
    state_store.write_json(_file_path, data, meta=False)


def _update(mutator):
    """Cross-process atomic cache transaction; corrupt input is never replaced."""
    def wrapped(data: dict):
        if data.get("schema") != 1:
            raise state_store.StateStoreError("data_cache.json: unsupported schema")
        data.setdefault("groups", {})
        data.setdefault("versions", {})
        result = mutator(data)
        data["saved_at"] = time.time()
        return result

    return state_store.update_json(_file_path, wrapped, default=_default(), meta=False)


# ── 对外 API ────────────────────────────────────────────────

def snapshot() -> dict:
    """整份缓存（供内容层/健康检查只读）。"""
    with _lock:
        return json.loads(json.dumps(_load_unlocked(), ensure_ascii=False))


def get_group(group: str) -> Optional[dict]:
    with _lock:
        return json.loads(json.dumps(_load_unlocked().get("groups", {}).get(group) or {}, ensure_ascii=False)) or None


def record_success(group: str, value: Any, status: str = STATUS_FRESH,
                   meta: Optional[dict] = None, now: Optional[float] = None) -> None:
    """成功采集：更新 value / last_success / last_attempt / status。
    value 允许 0 / "" 等合法值；None 不写（视为缺失）。"""
    t = now if now is not None else time.time()
    with _lock:
        def mutate(data):
            g = data["groups"].setdefault(group, {})
            if value is not None:
                g["value"] = value
                g["last_success"] = t
            g["last_attempt"] = t
            g["status"] = status
            if meta is not None:
                g["meta"] = dict(meta)
        try:
            _update(mutate)
        except state_store.StateStoreError as exc:
            logger.error("data_cache success write paused: %s", exc)


def record_failure(group: str, now: Optional[float] = None,
                   note: Optional[str] = None) -> str:
    """采集失败：只更新 last_attempt 与状态；value/last_success 保持不变。
    返回更新后的状态：有旧值 → stale；从未成功 → unknown。"""
    t = now if now is not None else time.time()
    with _lock:
        def mutate(data):
            g = data["groups"].setdefault(group, {})
            g["last_attempt"] = t
            has_value = g.get("value") is not None
            g["status"] = STATUS_STALE if has_value else STATUS_UNKNOWN
            if note:
                g["meta"] = dict(g.get("meta") or {})
                g["meta"]["last_fail"] = note
            return g["status"]
        try:
            return _update(mutate)
        except state_store.StateStoreError as exc:
            logger.error("data_cache failure write paused: %s", exc)
            return STATUS_UNKNOWN


def mark_unknown(group: str, now: Optional[float] = None) -> None:
    """显式置 unknown（如数据源确认不可用）。"""
    t = now if now is not None else time.time()
    with _lock:
        def mutate(data):
            g = data["groups"].setdefault(group, {})
            g["last_attempt"] = t
            g["status"] = STATUS_UNKNOWN
        try:
            _update(mutate)
        except state_store.StateStoreError as exc:
            logger.error("data_cache unknown write paused: %s", exc)


def expire(group: str, now: Optional[float] = None) -> None:
    """按外部判定过期：保留 value，状态 fresh→stale。"""
    t = now if now is not None else time.time()
    with _lock:
        def mutate(data):
            g = data["groups"].get(group)
            if g and g.get("status") == STATUS_FRESH:
                g["status"] = STATUS_STALE
                g["last_attempt"] = t
        try:
            _update(mutate)
        except state_store.StateStoreError as exc:
            logger.error("data_cache expire write paused: %s", exc)


def group_status(group: str, max_age: Optional[float] = None, now: Optional[float] = None) -> str:
    """读取当前状态；max_age 给定且 last_success 过旧时视作 stale。"""
    t = now if now is not None else time.time()
    g = get_group(group) or {}
    st = g.get("status", STATUS_UNKNOWN)
    ls = g.get("last_success")
    if st == STATUS_FRESH and max_age is not None and (ls is None or t - float(ls) > max_age):
        return STATUS_STALE
    return st


# ── 版本（AI key / news / gold / 新鲜度时间，互不影响）────────
def get_versions() -> dict:
    with _lock:
        return dict(_load_unlocked().get("versions", {}))


def bump_version(key: str, seed: Any) -> str:
    """key 内容变化时更新版本。seed 应为该组内容的关键字段快照（去采集时间）。"""
    import hashlib
    digest = hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    with _lock:
        def mutate(data):
            data["versions"][key] = digest
            return digest
        try:
            return _update(mutate)
        except state_store.StateStoreError as exc:
            logger.error("data_cache version write paused: %s", exc)
            return digest


def version(key: str) -> Optional[str]:
    with _lock:
        return _load_unlocked().get("versions", {}).get(key)
