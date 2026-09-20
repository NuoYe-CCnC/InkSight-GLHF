#!/usr/bin/env python3
"""Offline InkSight configuration utility; all output is secret-safe."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "shared" / "backend"
sys.path.insert(0, str(BACKEND))

from core import operator_config as config  # noqa: E402


def _paths(args) -> dict:
    return {
        "config_path": Path(args.config),
        "secrets_path": Path(args.secrets),
        "legacy_path": Path(args.legacy),
    }


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_init(args) -> int:
    result = config.init_files(config_path=Path(args.config), secrets_path=Path(args.secrets),
                               legacy_path=Path(args.legacy))
    _print(result)
    return 0


def command_validate(args) -> int:
    effective = config.load_effective(**_paths(args), use_last_known_good=False)
    summary = config.redacted_summary(effective)
    _print({
        "ok": True,
        "schema_version": summary["schema_version"],
        "fingerprint": summary["fingerprint"],
        "sources": summary["sources"],
        "warnings": summary["warnings"],
        "secret_status": summary["secret_status"],
    })
    return 0


def command_show(args) -> int:
    _print(config.redacted_summary(config.load_effective(**_paths(args))))
    return 0


def command_migrate(args) -> int:
    if args.apply and args.dry_run:
        raise config.ConfigError(["choose either --dry-run or --apply, not both"])
    if not args.apply and not args.dry_run:
        raise config.ConfigError(["migrate requires --dry-run or --apply"])
    result = config.migrate_legacy(
        legacy_path=Path(args.legacy), config_path=Path(args.config),
        secrets_path=Path(args.secrets), apply=args.apply,
        confirm_activate=args.confirm_activate,
        backup_dir=Path(args.backup_dir),
    )
    _print(result)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="InkSight versioned configuration utility")
    p.set_defaults(func=None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(config.CONFIG_FILE))
    common.add_argument("--secrets", default=str(config.SECRETS_FILE))
    common.add_argument("--legacy", default=str(config.LEGACY_FILE))
    sub = p.add_subparsers(dest="command")
    sub.add_parser("init", parents=[common],
                   help="initialize a new user; legacy users must migrate").set_defaults(func=command_init)
    sub.add_parser("validate", parents=[common], help="validate without activating").set_defaults(func=command_validate)
    sub.add_parser("show-effective", parents=[common], help="show fully redacted effective configuration").set_defaults(func=command_show)
    migrate = sub.add_parser("migrate", parents=[common], help="migrate legacy manual_settings")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--confirm-activate", action="store_true")
    migrate.add_argument("--backup-dir", default=str(config.BACKUP_DIR))
    migrate.set_defaults(func=command_migrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.func is None:
        parser().print_help()
        return 2
    try:
        return int(args.func(args))
    except config.ConfigError as exc:
        _print({"ok": False, "errors": exc.errors, "secrets": "not printed"})
        return 2
    except (OSError, ValueError) as exc:
        _print({"ok": False, "errors": [f"{type(exc).__name__}: {exc}"],
                "secrets": "not printed"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
