"""Turn a saved email into a page that looks like it does in a mail app.

Safety, the same way Mail / Gmail do it:
- no scripts: they're stripped here, and the page is served with a Content-Security-Policy that
  forbids them and shown in a sandboxed frame;
- remote images (which let senders see that you opened the email) stay blocked until you choose
  "Load images"; pictures embedded inside the email itself are shown right away;
- links open in a new browser tab.
"""
from __future__ import annotations

import base64
import email as email_lib
import re
from email import policy
from html import escape

PAGE_STYLE = """
html { background: #fff; }
body { margin: 18px 22px; font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
       sans-serif; color: #1d1c1a; background: #fff; overflow-wrap: anywhere; }
img { max-width: 100%; height: auto; }
table { max-width: 100% !important; }
a { color: #1a5fb4; }
.plain { white-space: pre-wrap; }
"""

REMOTE_RE = re.compile(r"""(?:src|background)\s*=\s*["']?\s*https?:|url\(\s*["']?https?:""", re.I)
MAX_INLINE_IMAGE = 3 * 1024 * 1024


def _sanitize(html: str) -> str:
    html = re.sub(r"(?is)<(script|iframe|object|embed|applet|frameset|frame|meta\s+http-equiv)[^>]*>.*?</\1\s*>", "", html)
    html = re.sub(r"(?is)<(script|iframe|object|embed|applet|base|meta\s+http-equiv)[^>]*/?>", "", html)
    html = re.sub(r"""(?i)\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", html)          # onclick=… etc.
    html = re.sub(r"""(?i)(href|src|action)\s*=\s*(["'])\s*(?:javascript|vbscript):[^"']*\2""", r'\1="#"', html)
    html = re.sub(r"(?is)<form\b", "<div", html)
    html = re.sub(r"(?is)</form\s*>", "</div>", html)
    return html


def _inline_images(msg) -> dict[str, str]:
    """cid:… references -> data: URIs for pictures that travel inside the email."""
    out = {}
    for part in msg.walk():
        cid = part.get("Content-ID")
        if not cid or not part.get_content_type().startswith("image/"):
            continue
        data = part.get_payload(decode=True) or b""
        if len(data) > MAX_INLINE_IMAGE:
            continue
        out[cid.strip("<> ")] = f"data:{part.get_content_type()};base64,{base64.b64encode(data).decode()}"
    return out


def _linkify(text: str) -> str:
    out = escape(text)
    return re.sub(r"(https?://[^\s<>&\"]+)", r'<a href="\1">\1</a>', out)


def email_page(raw: bytes | None, fallback_text: str, prefer_plain: bool = False) -> tuple[str, dict]:
    """Returns (html page, info) where info = {"has_html": bool, "has_remote_images": bool}."""
    html_body, text_body = None, fallback_text
    if raw:
        msg = email_lib.message_from_bytes(raw, policy=policy.default)
        html_part = msg.get_body(preferencelist=("html",))
        text_part = msg.get_body(preferencelist=("plain",))
        if text_part is not None:
            try:
                text_body = text_part.get_content()
            except Exception:
                pass
        if html_part is not None:
            try:
                html_body = html_part.get_content()
            except Exception:
                html_body = None
            if html_body:
                for cid, uri in _inline_images(msg).items():
                    html_body = re.sub(rf"cid:{re.escape(cid)}", uri, html_body, flags=re.I)
    info = {"has_html": bool(html_body), "has_remote_images": bool(html_body and REMOTE_RE.search(html_body))}
    if html_body and not prefer_plain:
        content = _sanitize(html_body)
    else:
        content = f'<div class="plain">{_linkify(text_body or "")}</div>'
    page = (f'<!doctype html><html><head><meta charset="utf-8"><base target="_blank">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<style>{PAGE_STYLE}</style></head><body>{content}</body></html>")
    return page, info


def csp(allow_remote: bool) -> str:
    """No scripts ever; remote images/styles/fonts only when the viewer allows them."""
    remote = " https: http:" if allow_remote else ""
    return ("default-src 'none'; script-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'; "
            f"img-src data:{remote}; style-src 'unsafe-inline'{remote}; font-src data:{remote}; "
            "base-uri 'none'")
