# state_store.py — persistent state isolation and crash-safe JSON transactions
"""Shared persistence primitives for paid/scheduled/background state.

State lives under ``<backend>/state`` (or ``INK_STATE_DIR``) and never under
``runtime_uploads``. Writes use a unique same-directory temporary file,
``fsync`` and ``os.replace``. A valid previous generation is retained as
``<name>.bak``. Advisory file locks plus per-process locks serialize readers
and read/modify/write transactions across workers and working directories.

Missing/corrupt files are reported to callers; this module never invents an
empty paid-budget, authorization, schedule or catch-up state.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

try:
    import fcntl  # type: ignore
except ImportError:  # Windows
    fcntl = None
    import msvcrt  # type: ignore

_BACKEND = Path(__file__).resolve().parent.parent
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
T = TypeVar("T")


class StateStoreError(RuntimeError):
    """Raised when a transactional state mutation cannot safely continue."""


def state_dir() -> Path:
    env = os.environ.get("INK_STATE_DIR")
    d = Path(env).expanduser() if env else (_BACKEND / "state")
    if not d.is_absolute():
        d = (_BACKEND / d).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(name: str) -> Path:
    if not name or Path(name).name != name:
        raise ValueError("state name must be one filename")
    return state_dir() / name


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def file_lock(path: Path, *, operation: str = "state") -> Iterator[None]:
    """Exclusive process/thread lock associated with ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.{operation}.lock")
    thread_lock = _thread_lock(lock_path)
    with thread_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:  # pragma: no cover - exercised on Windows distribution
                if os.path.getsize(lock_path) == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)


def _decode(path: Path) -> tuple[object | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"io:{type(exc).__name__}"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"corrupt:{exc}"


def read_json(path: Path) -> tuple[object | None, str | None]:
    """Return ``(data, error)``; never silently restores or initializes."""
    path = Path(path)
    with file_lock(path):
        return _decode(path)


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_bytes(path: Path, payload: bytes, *, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _write_locked(path: Path, obj: dict, *, meta: bool, keep_backup: bool) -> None:
    value = copy.deepcopy(obj)
    if meta:
        value.setdefault("v", 1)
        value["updated_at"] = int(time.time())
    if keep_backup and path.exists():
        previous = path.read_bytes()
        try:
            json.loads(previous.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        else:
            _replace_bytes(backup_path(path), previous, prefix=f".{path.name}.bak.")
    encoded = json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")
    _replace_bytes(path, encoded, prefix=f".{path.name}.tmp.")


def write_json(path: Path, obj: dict, meta: bool = True, *, keep_backup: bool = True) -> None:
    """Crash-safe replacement with locking and a previous-valid backup."""
    path = Path(path)
    with file_lock(path):
        _write_locked(path, obj, meta=meta, keep_backup=keep_backup)


def remove_json(path: Path, *, keep_backup: bool = True) -> bool:
    """Remove a valid state file under lock, retaining its last value as backup."""
    path = Path(path)
    with file_lock(path):
        current, error = _decode(path)
        if error == "missing":
            return False
        if error or not isinstance(current, dict):
            raise StateStoreError(f"{path.name}: {error or 'root is not an object'}")
        if keep_backup:
            previous = path.read_bytes()
            _replace_bytes(backup_path(path), previous, prefix=f".{path.name}.bak.")
        path.unlink()
        _fsync_dir(path.parent)
        return True


def update_json(
    path: Path,
    updater: Callable[[dict], T],
    *,
    default: dict | None = None,
    meta: bool = True,
    keep_backup: bool = True,
) -> T:
    """Atomically read, mutate and replace one JSON object.

    ``default`` is used only for explicitly safe-to-initialize state. Without
    it, missing or corrupt input raises ``StateStoreError``.
    """
    path = Path(path)
    with file_lock(path):
        current, error = _decode(path)
        if error:
            if error == "missing" and default is not None:
                current = copy.deepcopy(default)
            else:
                raise StateStoreError(f"{path.name}: {error}")
        if not isinstance(current, dict):
            raise StateStoreError(f"{path.name}: root is not an object")
        result = updater(current)
        _write_locked(path, current, meta=meta, keep_backup=keep_backup)
        return result
