# news_budget.py — 新闻调用费用遥测账本（不参与执行决策）
"""保留历史费用/调用统计，但取消 10/8/9 元的运行门槛。

旧配置和旧账本继续可读；缺失、损坏或金额超过旧阈值都不会阻止新闻生成。
真正的执行上限由每期输入/输出 token 与最多两次模型调用共同保证。
"""
from __future__ import annotations
from . import state_store

import time
from pathlib import Path

_FILE = state_store.state_path("news_budget.json")
MONTH_LIMIT = 10.0
WARN_AT = 8.0
STOP_AT = 9.0  # ≥9 停止（保留 1 元缓冲）


def configure_limit(month_limit: float | None) -> None:
    """Compatibility only: retain legacy numbers for reporting, never gating."""
    global MONTH_LIMIT, WARN_AT, STOP_AT
    if month_limit is None:
        return
    MONTH_LIMIT = max(0.01, float(month_limit))
    WARN_AT = round(MONTH_LIMIT * 0.8, 4)
    STOP_AT = round(MONTH_LIMIT * 0.9, 4)


def configure_file(path) -> None:
    global _FILE
    _FILE = Path(path)


def _month() -> str:
    return time.strftime("%Y-%m", time.localtime())


def _load() -> dict:
    d, error = state_store.read_json(_FILE)
    if error == "missing":
        return {"months": {}, "_missing": True}   # 保守：缺失≠零消费
    if error or not isinstance(d, dict):
        return {"months": {}, "_corrupt": True}
    return d


def initialize_empty(note: str = "新装/核账后初始化") -> dict:
    """仅供运维在核实后显式初始化空账本；日常缺失一律暂停。"""
    d = {"months": {}, "note": note, "restored_at": int(time.time())}
    _save(d)
    return snapshot()


def _save(d: dict) -> None:
    state_store.write_json(_FILE, d)


def _month_state() -> dict:
    d = _load()
    if d.get("_missing") or d.get("_corrupt"):
        return d   # 缺失/损坏：向上抛给调用方按保守规则处理
    m = d["months"].setdefault(_month(), {"spent": 0.0, "reserved": 0.0,
                                          "calls": 0, "warned": False})
    return m


def snapshot() -> dict:
    """返回账本快照（金额两位小数）；账本缺失/损坏时明确标记，不假装为 0。"""
    m = _month_state()
    if m.get("_missing") or m.get("_corrupt"):
        return {"month": _month(), "spent": None, "reserved": None, "calls": None,
                "warned": False, "month_limit": MONTH_LIMIT, "warn_at": WARN_AT,
                "stop_at": STOP_AT, "enforced": False, "policy": "telemetry_only",
                "error": "ledger-missing" if m.get("_missing") else "ledger-corrupt",
                "note": "费用账本缺失/损坏：统计不完整，但不会阻止新闻生成"}
    return {"month": _month(), "spent": round(m["spent"], 2),
            "reserved": round(m["reserved"], 2),
            "calls": m["calls"], "warned": bool(m.get("warned")),
            "month_limit": MONTH_LIMIT, "warn_at": WARN_AT, "stop_at": STOP_AT,
            "enforced": False, "policy": "telemetry_only"}


def can_request() -> tuple[bool, str]:
    """Legacy API: the former monetary gate is deliberately disabled."""
    m = _month_state()
    if m.get("_missing") or m.get("_corrupt"):
        return True, "ok:telemetry-incomplete"
    return True, "ok:monetary-gate-disabled"


def reserve(amount: float) -> bool:
    """Best-effort telemetry reservation; always authorizes execution."""
    loaded = _load()
    if loaded.get("_missing") or loaded.get("_corrupt"):
        return True

    def update(d: dict) -> bool:
        months = d.setdefault("months", {})
        m = months.setdefault(_month(), {"spent": 0.0, "reserved": 0.0,
                                          "calls": 0, "warned": False})
        requested = max(0.0, float(amount))
        m["reserved"] = round(float(m.get("reserved", 0.0)) + requested, 4)
        return True

    try:
        return bool(state_store.update_json(_FILE, update))
    except state_store.StateStoreError:
        return True


def settle(actual_cost: float, release: float = 0.0) -> dict:
    """按实际费用结算（重试/修订也走此路径）。release=本次调用预留的全额，
    无论成功/失败/超时都释放该笔预留；actual 计花费（费用不明=release 全额保守记账）。
    账本缺失/损坏时拒绝结算落盘（避免把丢失账本重建为 0）。"""
    def update(d: dict) -> None:
        m = d.setdefault("months", {}).setdefault(
            _month(), {"spent": 0.0, "reserved": 0.0, "calls": 0, "warned": False})
        actual = max(0.0, float(actual_cost))
        rel = max(actual, float(release))
        m["reserved"] = round(max(0.0, float(m.get("reserved", 0.0)) - rel), 4)
        m["spent"] = round(float(m.get("spent", 0.0)) + actual, 4)
        m["calls"] = int(m.get("calls", 0)) + 1
        if m["spent"] >= WARN_AT and not m.get("warned"):
            m["warned"] = True

    loaded = _load()
    if loaded.get("_missing") or loaded.get("_corrupt"):
        return snapshot()
    try:
        state_store.update_json(_FILE, update)
    except state_store.StateStoreError:
        return snapshot()
    return snapshot()
