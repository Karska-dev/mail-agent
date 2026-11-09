"""Secure connections that also work with the python.org installer on a Mac.

That installer ships without trusted certificates until you run "Install Certificates.command",
so HTTPS / IMAP connections fail with CERTIFICATE_VERIFY_FAILED. If that's the case we export
the Mac's own trusted root certificates once and use those instead.
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_CACHE = Path(os.environ.get("EMAIL_AGENT_DATA", "~/.email_agent")).expanduser() / "macos-roots.pem"


def _has_default_certs() -> bool:
    ctx = ssl.create_default_context()
    return bool(ctx.get_ca_certs()) or bool(ctx.cert_store_stats().get("x509_ca"))


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # present if the user ran Install Certificates or pip installed it
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    if sys.platform == "darwin" and not _has_default_certs():
        if not _CACHE.exists():
            pem = subprocess.run(
                ["security", "find-certificate", "-a", "-p",
                 "/System/Library/Keychains/SystemRootCertificates.keychain"],
                capture_output=True, text=True).stdout
            if pem:
                _CACHE.parent.mkdir(parents=True, exist_ok=True)
                _CACHE.write_text(pem)
        if _CACHE.exists():
            return ssl.create_default_context(cafile=str(_CACHE))
    return ssl.create_default_context()
