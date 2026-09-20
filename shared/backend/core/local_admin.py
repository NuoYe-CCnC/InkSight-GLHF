"""Loopback bootstrap and local recovery primitives for the root account."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import aiosqlite

from . import config_store, db as db_module

_USERNAME_RE = re.compile(r"^[^\s]{2,30}$")


class AdminSetupError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _validate(username: str, password: str) -> tuple[str, str]:
    username = (username or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise AdminSetupError("invalid_username", "用户名须为 2–30 个不含空格的字符。")
    if len(password or "") < 12:
        raise AdminSetupError("weak_password", "密码至少需要 12 位。")
    return username, password


def _db_path(path: Path | str | None) -> Path:
    return Path(path) if path is not None else Path(db_module._MAIN_DB_PATH)


async def root_exists(*, db_path: Path | str | None = None) -> bool:
    async with aiosqlite.connect(_db_path(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM users WHERE role = 'root' LIMIT 1"
        )
        return await cursor.fetchone() is not None


async def create_first_root(
    username: str,
    password: str,
    *,
    db_path: Path | str | None = None,
) -> int:
    """Create exactly one first root, serialized with BEGIN IMMEDIATE."""
    username, password = _validate(username, password)
    path = _db_path(db_path)
    password_hash, _salt = config_store._hash_password(password)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA busy_timeout=5000")
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT 1 FROM users WHERE role = 'root' LIMIT 1"
            )
            if await cursor.fetchone() is not None:
                raise AdminSetupError("root_exists", "管理员已经存在，不能重复初始化。")
            cursor = await conn.execute(
                "SELECT 1 FROM users WHERE username = ? LIMIT 1", (username,)
            )
            if await cursor.fetchone() is not None:
                raise AdminSetupError("username_exists", "该用户名已存在，请换一个用户名。")
            cursor = await conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, ?, 'root', ?)
                """,
                (username, password_hash, datetime.now().isoformat()),
            )
            await conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            await conn.rollback()
            raise


async def reset_root_password(
    username: str,
    password: str,
    *,
    db_path: Path | str | None = None,
) -> None:
    """Local-filesystem recovery operation; never exposed as an HTTP route."""
    username, password = _validate(username, password)
    password_hash, _salt = config_store._hash_password(password)
    async with aiosqlite.connect(_db_path(db_path)) as conn:
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ? AND role = 'root'",
            (password_hash, username),
        )
        if cursor.rowcount != 1:
            await conn.rollback()
            raise AdminSetupError("root_not_found", "没有找到该 root 管理员。")
        await conn.commit()
