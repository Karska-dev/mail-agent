"""Password storage.

On a Mac, passwords go into the macOS Keychain (the same place Safari and Mail keep theirs),
via the built-in `security` tool — nothing extra to install. You can see or delete them in
the Keychain Access app under the name "Mail Agent".

On other systems they fall back to ~/.email_agent/secrets.json, readable only by you.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SERVICE = "Mail Agent"
_FALLBACK = Path(os.environ.get("EMAIL_AGENT_DATA", "~/.email_agent")).expanduser() / "secrets.json"


def _mac() -> bool:
    return sys.platform == "darwin"


def get(key: str) -> str | None:
    if _mac():
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", key, "-w"],
                           capture_output=True, text=True)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    if _FALLBACK.exists():
        return json.loads(_FALLBACK.read_text() or "{}").get(key)
    return None


def put(key: str, secret: str) -> None:
    if _mac():
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", key, "-w", secret],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Couldn't save to the Keychain: {r.stderr.strip()}")
        return
    data = json.loads(_FALLBACK.read_text() or "{}") if _FALLBACK.exists() else {}
    data[key] = secret
    _FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    _FALLBACK.write_text(json.dumps(data))
    os.chmod(_FALLBACK, 0o600)


def delete(key: str) -> None:
    if _mac():
        subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", key], capture_output=True)
        return
    if _FALLBACK.exists():
        data = json.loads(_FALLBACK.read_text() or "{}")
        data.pop(key, None)
        _FALLBACK.write_text(json.dumps(data))


def where() -> str:
    return "your Mac's Keychain" if _mac() else str(_FALLBACK)
