"""Deterministic local fallback; corpus publication rights are audited separately."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import news_digest

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "daily_messages.json"


def load_messages(path: Path | None = None) -> list[dict]:
    data = json.loads((path or DATA_FILE).read_text(encoding="utf-8"))
    rows = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("daily-message corpus is empty")
    result = []
    ids = set()
    for row in rows:
        ident = str((row or {}).get("id") or "")
        text = str((row or {}).get("text") or "").strip()
        checked = news_digest.validate_digest_text(text)
        if not ident or ident in ids or not checked.get("ok"):
            raise ValueError(f"invalid daily message: {ident or '<missing-id>'}")
        ids.add(ident)
        result.append({"id": ident, "text": text, "lines": checked["lines"]})
    return result


def choose(day: str, schedule_id: str, recent_ids: list[str] | None = None) -> dict:
    rows = load_messages()
    seed = hashlib.sha256(f"{day}|{schedule_id}|daily-message-v1".encode()).digest()
    start = int.from_bytes(seed[:8], "big") % len(rows)
    recent = {str(value) for value in (recent_ids or [])[-4:]}
    for offset in range(len(rows)):
        row = rows[(start + offset) % len(rows)]
        if row["id"] not in recent or offset == len(rows) - 1:
            return dict(row)
    return dict(rows[start])


def version(day: str, schedule_id: str, message_id: str) -> str:
    return hashlib.md5(f"{day}|{schedule_id}|{message_id}".encode()).hexdigest()[:16]
