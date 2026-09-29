#!/usr/bin/env python3
"""Install the public source template into per-user data without overwriting it.

The desktop app ships a read-only, privacy-scanned source template and a
relocatable interpreter.  InkSight's existing backend resolves several paths
relative to its source tree, so the runtime tree lives in Application Support.
Every version has its own tree; switching the current pointer is the commit.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-test\.[0-9]+$")
DATA_FILES = (
    "shared/backend/.env", "shared/backend/.jwt_secret",
    "shared/backend/inksight.db", "shared/backend/cache.db",
    "shared/backend/stats.db", "shared/backend/static.db",
    "shared/backend/data/member_config.json",
    "shared/tools/.cloud_publish_state.json",
    "shared/tools/.cloud_publish_state.json.bak",
)
DATA_DIRS = (
    "shared/backend/state", "shared/backend/runtime_uploads",
    "shared/config/backups",
)
CONFIG_NAMES = (
    "inksight_config.json", "inksight_secrets.json", "manual_settings.json",
)


def _private_root(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


@contextmanager
def _lock(root: Path):
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".current-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _current(root: Path) -> tuple[str, Path] | None:
    pointer = root / "current.json"
    if not pointer.is_file():
        return None
    value = json.loads(pointer.read_text(encoding="utf-8"))
    version = value.get("version", "")
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid current version marker")
    target = root / "versions" / version / "source"
    if not target.is_dir():
        raise ValueError("current runtime directory is missing")
    return version, target


def _no_symlink_copy(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ValueError("import source contains a symbolic link")
    if source.is_dir():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for child in source.iterdir():
            _no_symlink_copy(child, target / child.name)
    elif source.is_file():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    else:
        raise ValueError("import source contains an unsupported file type")


def _copy_user_data(source: Path, target: Path) -> list[str]:
    copied: list[str] = []
    for rel in DATA_FILES:
        original = source / rel
        if original.is_file():
            _no_symlink_copy(original, target / rel)
            copied.append(rel)
    for rel in DATA_DIRS:
        original = source / rel
        if original.is_dir():
            _no_symlink_copy(original, target / rel)
            copied.append(rel + "/")
    for name in CONFIG_NAMES:
        rel = "shared/config/" + name
        original = source / rel
        if original.is_file():
            _no_symlink_copy(original, target / rel)
            copied.append(rel)
    return copied


def _make_owned_runtime_writable(runtime: Path) -> None:
    """A copied template may inherit read-only .app permissions."""
    for path in (runtime, *runtime.rglob("*")):
        if path.is_symlink():
            raise ValueError("bundled source template unexpectedly contains a symbolic link")
        bits = path.stat().st_mode
        path.chmod(bits | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))


def _assert_no_symlinks(runtime: Path) -> None:
    if runtime.is_symlink() or any(path.is_symlink() for path in runtime.rglob("*")):
        raise ValueError("runtime contains a symbolic link; refusing to follow it")


def _ensure_env(runtime: Path) -> None:
    env = runtime / "shared/backend/.env"
    if env.exists():
        return
    example = runtime / "shared/backend/.env.example"
    content = example.read_text(encoding="utf-8")
    token = secrets.token_urlsafe(32)
    rows = [f"ADMIN_TOKEN={token}" if row.startswith("ADMIN_TOKEN=") else row
            for row in content.splitlines()]
    env.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(env, 0o600)


def _initialize_blank_config(runtime: Path) -> None:
    directory = runtime / "shared/config"
    public = directory / "inksight_config.json"
    secret = directory / "inksight_secrets.json"
    legacy = directory / "manual_settings.json"
    if legacy.exists():
        return  # never let blank defaults shadow an imported legacy file
    if public.exists() != secret.exists():
        raise ValueError("only one configuration file exists; refusing to fill the partial pair")
    if public.exists():
        return
    for name, target in (("inksight_config.example.json", public),
                         ("inksight_secrets.example.json", secret)):
        example = directory / name
        if not example.is_file():
            raise ValueError("bundled configuration example is missing")
        # Validate syntax before any real file is created.
        json.loads(example.read_text(encoding="utf-8"))
        _no_symlink_copy(example, target)


def _validate_config(runtime: Path) -> None:
    tool = runtime / "shared/tools/inksight_config.py"
    if not tool.is_file():
        return  # small test fixtures; public release candidate always contains it
    result = subprocess.run([sys.executable, str(tool), "validate"], cwd=runtime,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=20, check=False)
    if result.returncode != 0:
        raise ValueError("configuration validation failed; previous runtime was left unchanged")


def ensure_install(template: Path, data_root: Path, version: str) -> dict:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid app version")
    template = Path(template).resolve()
    if not (template / ".inksight-source-candidate").is_file():
        raise ValueError("bundled source template is not a verified candidate")
    _assert_no_symlinks(template)
    root = _private_root(data_root)
    with _lock(root):
        previous = _current(root)
        if previous and previous[0] == version:
            return {"status": "already-installed", "version": version,
                    "runtime": str(previous[1])}
        versions = root / "versions"
        versions.mkdir(mode=0o700, exist_ok=True)
        target_parent = versions / version
        if target_parent.exists():
            raise ValueError("target version directory already exists without current pointer")
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=versions))
        try:
            runtime = staging / "source"
            shutil.copytree(template, runtime, symlinks=False)
            _make_owned_runtime_writable(runtime)
            if previous:
                _assert_no_symlinks(previous[1])
            copied = _copy_user_data(previous[1], runtime) if previous else []
            _ensure_env(runtime)
            _initialize_blank_config(runtime)
            _validate_config(runtime)
            os.replace(staging, target_parent)
            _atomic_json(root / "current.json", {"schema": 1, "version": version})
            return {"status": "installed", "version": version,
                    "runtime": str(target_parent / "source"), "preserved": copied}
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def import_existing(source: Path, data_root: Path, *, confirm: bool = False) -> dict:
    source = Path(source).expanduser().resolve()
    if not (source / "shared/backend").is_dir() or not (source / "shared/config").is_dir():
        raise ValueError("selected folder is not an InkSight source root")
    root = _private_root(data_root)
    with _lock(root):
        current = _current(root)
        if current is None:
            raise ValueError("install the app before importing old data")
        version, runtime = current
        if source == runtime or runtime in source.parents or source in runtime.parents:
            raise ValueError("cannot import the active app runtime")
        proposed = [rel for rel in DATA_FILES if (source / rel).is_file()]
        proposed += [rel + "/" for rel in DATA_DIRS if (source / rel).is_dir()]
        proposed += ["shared/config/" + name for name in CONFIG_NAMES
                     if (source / "shared/config" / name).is_file()]
        if not confirm:
            return {"status": "dry-run", "items": proposed, "version": version}
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=runtime.parent))
        backup = runtime.parent / ("source-before-import-" + secrets.token_hex(6))
        try:
            candidate = staging / "source"
            _assert_no_symlinks(runtime)
            shutil.copytree(runtime, candidate, symlinks=False)
            _make_owned_runtime_writable(candidate)
            old_legacy_only = (source / "shared/config/manual_settings.json").is_file() and not (
                source / "shared/config/inksight_config.json").exists() and not (
                source / "shared/config/inksight_secrets.json").exists()
            if old_legacy_only:
                for name in ("inksight_config.json", "inksight_secrets.json"):
                    (candidate / "shared/config" / name).unlink(missing_ok=True)
            copied = _copy_user_data(source, candidate)
            _ensure_env(candidate)
            _initialize_blank_config(candidate)
            _validate_config(candidate)
            os.replace(runtime, backup)
            try:
                os.replace(candidate, runtime)
            except BaseException:
                os.replace(backup, runtime)
                raise
            return {"status": "imported", "items": copied,
                    "backup": str(backup), "version": version}
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    action = parser.add_subparsers(dest="action", required=True)
    install = action.add_parser("install")
    install.add_argument("--template", type=Path, required=True)
    install.add_argument("--version", required=True)
    old = action.add_parser("import")
    old.add_argument("--source", type=Path, required=True)
    old.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    result = (ensure_install(args.template, args.data_root, args.version)
              if args.action == "install" else
              import_existing(args.source, args.data_root, confirm=args.confirm))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
