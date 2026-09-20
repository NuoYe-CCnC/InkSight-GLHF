"""Deterministic news time, event-type, evidence and cross-issue dedupe rules.

This module is deliberately model-independent.  A model may improve wording,
but cannot promote an application case into a launch, invent an event date, or
make an already-published event fresh again.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Iterable


EVENT_TYPES = ("launch", "update", "application", "research", "plan", "test", "rumor")
HISTORY_KEEP_SECONDS = 21 * 86400
HISTORY_MAX_ITEMS = 240

_LAUNCH = ("发布", "推出", "上线", "正式开放", "release", "launch", "introduc", "available now")
_UPDATE = ("更新", "升级", "新增", "扩大", "第二阶段", "update", "upgrade", "adds ", "expands ")
_APPLICATION = ("用于", "应用于", "借助", "帮助", "采用", "use case", "helps ", "using ", "powered by")
_RESEARCH = ("研究", "论文", "实验", "发现", "research", "paper", "experiment", "study")
_PLAN = ("计划", "预计", "将于", "拟", "plan", "will ", "expected", "roadmap")
_TEST = ("测试", "试用", "灰度", "预览", "beta", "preview", "pilot", "trial")
_RUMOR = ("传闻", "据悉", "或将", "爆料", "rumor", "reportedly", "may ")

_GENERIC = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "how",
    "helps", "using", "use", "new", "最新", "发布", "推出", "更新", "研究", "实验",
    "应用", "进行", "通过", "一个", "一种", "公司", "模型", "技术",
}


def _text(candidate: dict) -> str:
    return f"{candidate.get('title') or ''} {candidate.get('summary') or ''}".strip()


def infer_event_type(candidate: dict) -> str:
    """Classify what actually happened; application/research outrank launch words.

    Article titles often contain a model name whose original launch is old.  A
    current case study about that model must therefore not become a new launch.
    """
    supplied = str(candidate.get("event_type") or "").lower()
    if supplied in EVENT_TYPES:
        return supplied
    text = _text(candidate).lower()
    if any(word in text for word in _RUMOR):
        return "rumor"
    if any(word in text for word in _PLAN):
        return "plan"
    if any(word in text for word in _APPLICATION):
        return "application"
    if any(word in text for word in _RESEARCH):
        return "research"
    if any(word in text for word in _TEST):
        return "test"
    if any(word in text for word in _UPDATE):
        return "update"
    if any(word in text for word in _LAUNCH):
        return "launch"
    # Unknown event semantics are intentionally conservative.
    return "research" if candidate.get("source") in ("openai",) else "application"


def enrich_candidate(candidate: dict, *, now: int | None = None) -> dict:
    """Add explicit time provenance without substituting collection for publish."""
    now = int(time.time()) if now is None else int(now)
    row = dict(candidate)
    published = row.get("published_at", row.get("published"))
    collected = row.get("collected_at", row.get("collected"))
    event_at = row.get("event_at")
    issue_at = row.get("issue_at")
    row["published_at"] = int(published) if isinstance(published, (int, float)) and published > 0 else None
    row["collected_at"] = int(collected) if isinstance(collected, (int, float)) and collected > 0 else None
    row["event_at"] = int(event_at) if isinstance(event_at, (int, float)) and event_at > 0 else None
    row["issue_at"] = int(issue_at) if isinstance(issue_at, (int, float)) and issue_at > 0 else None
    row["trusted_at"] = row["published_at"]
    row["time_trust"] = "published" if row["published_at"] else "unknown"
    row["event_type"] = infer_event_type(row)
    row["age_seconds"] = max(0, now - row["published_at"]) if row["published_at"] else None
    return row


def freshness_band(candidate: dict, *, now: int, last_issue_at: int | None) -> int:
    """0=new since prior issue, 1=within 24h, 2=24-48h, 3=unknown/older."""
    published = candidate.get("published_at")
    if not isinstance(published, int) or published <= 0:
        return 3
    if last_issue_at and published > last_issue_at:
        return 0
    age = max(0, now - published)
    if age <= 24 * 3600:
        return 1
    if age <= 48 * 3600:
        return 2
    return 3


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    ascii_words = re.findall(r"[a-z]+(?:[-_.][a-z0-9]+)*|\d+(?:\.\d+)*", lowered)
    han_runs = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    han = []
    for run in han_runs:
        han.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return {token for token in ascii_words + han if token not in _GENERIC and len(token) > 1}


def title_similarity(a: str, b: str) -> float:
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def event_fingerprint(candidate: dict) -> str:
    """Stable fingerprint used for exact/same-URL history matches."""
    title = " ".join(sorted(_tokens(str(candidate.get("title") or ""))))
    url = re.sub(r"[?#].*$", "", str(candidate.get("url") or "")).rstrip("/")
    basis = title or url or str(candidate.get("article_id") or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _substantive_update(candidate: dict, old: dict) -> bool:
    if infer_event_type(candidate) != "update":
        return False
    new_tokens = _tokens(_text(candidate))
    old_tokens = _tokens(str(old.get("title") or ""))
    delta = new_tokens - old_tokens
    newer = int(candidate.get("published_at") or 0) > int(old.get("published_at") or 0)
    return newer and len(delta) >= 2


def history_relation(candidate: dict, history: Iterable[dict]) -> dict | None:
    fp = event_fingerprint(candidate)
    url = re.sub(r"[?#].*$", "", str(candidate.get("url") or "")).rstrip("/")
    for old in history:
        same = fp == old.get("fingerprint")
        old_url = re.sub(r"[?#].*$", "", str(old.get("url") or "")).rstrip("/")
        same = same or bool(url and old_url and url == old_url)
        similarity = title_similarity(str(candidate.get("title") or ""), str(old.get("title") or ""))
        same = same or similarity >= 0.58
        # A substantive update naturally adds several new terms and can fall
        # below the duplicate threshold; retain a lower relation threshold so
        # its lineage is still traceable via update_of.
        if infer_event_type(candidate) == "update":
            shared = _tokens(str(candidate.get("title") or "")) & _tokens(str(old.get("title") or ""))
            if similarity >= 0.25 and len(shared) >= 3:
                same = True
        if same:
            return {"duplicate": not _substantive_update(candidate, old),
                    "matched": old,
                    "update_of": old.get("fingerprint") or old.get("article_id")}
    return None


def duplicate_history_match(candidate: dict, history: Iterable[dict]) -> dict | None:
    relation = history_relation(candidate, history)
    return relation.get("matched") if relation and relation.get("duplicate") else None


def prune_history(history: Iterable[dict], *, now: int | None = None) -> list[dict]:
    now = int(time.time()) if now is None else int(now)
    rows = [dict(row) for row in history if now - int(row.get("issued_at") or 0) <= HISTORY_KEEP_SECONDS]
    rows.sort(key=lambda row: int(row.get("issued_at") or 0), reverse=True)
    return rows[:HISTORY_MAX_ITEMS]


def history_rows(events: Iterable[dict], *, issued_at: int) -> list[dict]:
    out = []
    for event in events:
        row = {
            "fingerprint": event.get("fingerprint") or event_fingerprint(event),
            "article_id": event.get("id") or event.get("article_id"),
            "source": event.get("source"), "url": event.get("url"),
            "title": event.get("title"), "event_type": event.get("event_type"),
            "published_at": event.get("published_at"), "event_at": event.get("event_at"),
            "trusted_at": event.get("trusted_at"), "update_of": event.get("update_of"),
            "issued_at": int(issued_at),
        }
        if row["article_id"] or row["url"] or row["title"]:
            out.append(row)
    return out


def _model_subjects(candidate: dict) -> set[str]:
    text = _text(candidate)
    subjects = set(re.findall(r"(?:GPT|DeepSeek|Claude|Gemini|Llama)[-\s]?[A-Za-z0-9.]+", text, re.I))
    return {subject.lower().replace(" ", "") for subject in subjects}


def unsupported_claims(body: str, candidates: Iterable[dict]) -> list[str]:
    """Reject high-risk promotion of application/research/plan into a launch."""
    compact = re.sub(r"\s+", "", body or "").lower()
    clauses = [re.sub(r"\s+", "", part).lower() for part in re.split(r"[。；！？\n]", body or "")]
    errors = []
    for candidate in candidates:
        event_type = infer_event_type(candidate)
        subjects = _model_subjects(candidate)
        if event_type in ("application", "research", "plan", "test", "rumor"):
            for subject in subjects:
                risky = [clause for clause in clauses if subject in clause and any(word in clause for word in _LAUNCH)]
                if risky:
                    errors.append(f"{candidate.get('article_id') or '?'}:{event_type}-promoted-to-launch")
                    break
        if event_type == "rumor" and not any(word in compact for word in ("传闻", "据", "尚未证实", "或将")):
            errors.append(f"{candidate.get('article_id') or '?'}:rumor-lost-qualification")
    return errors
