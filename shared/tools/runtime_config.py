"""Build optional host-sync settings from InkSight's existing private file."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


DEFAULT_SECRETS = Path(__file__).resolve().parents[1] / "config" / "inksight_secrets.json"


def from_operator_secrets(mac: str, *, backend: str = "http://127.0.0.1:8080",
                          path: Path = DEFAULT_SECRETS) -> dict:
    normalized = re.sub(r"[:-]", "", mac.strip()).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", normalized):
        raise ValueError("device MAC must contain exactly 12 hexadecimal digits")
    secret_file = Path(path)
    if os.name != "nt" and secret_file.stat().st_mode & 0o077:
        raise ValueError("inksight_secrets.json must be readable only by its owner (0600)")
    secrets = json.loads(secret_file.read_text(encoding="utf-8"))
    cloud = secrets.get("cloud") or {}
    webdav = str(cloud.get("base_url") or "").strip()
    user = str(cloud.get("user") or "").strip()
    password = str(cloud.get("password") or "")
    if not webdav.startswith("https://") or not user or not password:
        raise ValueError("configure cloud.base_url, cloud.user and cloud.password in inksight_secrets.json")
    if not backend.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("host-sync backend must be local loopback")
    return {"backend": backend, "webdav": webdav, "user": user,
            "password": password, "devices": [{"mac": normalized,
                                                  "modes": ["AI_USAGE"]}]}
