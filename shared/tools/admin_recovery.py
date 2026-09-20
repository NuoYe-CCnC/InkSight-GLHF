#!/usr/bin/env python3
"""Hidden-input local root initialization and password recovery utility."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from core import config_store, local_admin  # noqa: E402


def _password() -> str:
    first = getpass.getpass("新密码（至少 12 位，不会显示）：")
    second = getpass.getpass("再次输入新密码：")
    if first != second:
        raise local_admin.AdminSetupError("password_mismatch", "两次密码不一致。")
    return first


async def _run(action: str) -> None:
    await config_store.init_db()
    username = input("root 用户名：").strip()
    password = _password()
    phrase = "CREATE ROOT" if action == "init" else "RESET PASSWORD"
    confirmation = input(f"输入 {phrase} 确认：").strip()
    if confirmation != phrase:
        raise local_admin.AdminSetupError("not_confirmed", "未确认，操作已取消。")
    if action == "init":
        await local_admin.create_first_root(username, password)
        print("root 管理员已创建。")
    else:
        await local_admin.reset_root_password(username, password)
        print("root 密码已更新，现有会话将在到期后失效。")


def main() -> int:
    parser = argparse.ArgumentParser(description="InkSight 本机 root 恢复工具")
    parser.add_argument("action", choices=("init", "reset"))
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.action))
        return 0
    except (local_admin.AdminSetupError, EOFError, KeyboardInterrupt) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
