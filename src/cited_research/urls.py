"""Decide whether a cited-page URL is safe to render as a public, clickable link."""

from __future__ import annotations

import ipaddress
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".corp",
    ".intranet",
)
# NAT64 well-known prefix (RFC 6052): addresses here map onto embedded IPv4 hosts, including
# private ones, and `ipaddress` reports some of them as global.
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# Browsers resolve shorthand, hex and octal IPv4 forms (127.1, 0x7f.1, 0177.0.0.1) that
# `ipaddress` rejects. A TLD cannot be all-numeric (RFC 3696), so a host whose last label is
# all digits is an address, and a hex label is never a real hostname. All-digit labels elsewhere
# are legitimate (www.163.com), so they are allowed.
HEX_LABEL = re.compile(r"^0x[0-9a-f]+$")


def check_public_url(url: object) -> Tuple[bool, Optional[str]]:
    """Return (ok, issue). `issue` is a short machine-readable reason when ok is False.

    Rejects anything that is not an absolute http(s) URL on a public hostname. This is a link
    hygiene check, not a reachability or safety check of the page itself.
    """
    if not isinstance(url, str) or not url.strip():
        return False, "missing_or_not_a_string"
    if url != url.strip() or any(c.isspace() or ord(c) < 32 for c in url):
        return False, "contains_whitespace_or_control_chars"
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return False, "unparseable"
    if parts.scheme not in ("http", "https"):
        return False, "scheme_not_http"
    if not host:
        return False, "no_host"
    if parts.username is not None or parts.password is not None:
        return False, "embedded_credentials"
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(PRIVATE_HOST_SUFFIXES):
        return False, "private_hostname"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return False, "single_label_hostname"
        labels = host.split(".")
        if labels[-1].isdigit() or any(HEX_LABEL.match(label) for label in labels):
            return False, "numeric_hostname"
        return True, None
    if not ip.is_global or (ip.version == 6 and ip in NAT64_PREFIX):
        return False, "non_public_ip"
    return True, None


def strip_credentials(url: str) -> str:
    """Replace any userinfo (user:password@) with a marker. Other URL parts are kept as-is."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return parts._replace(netloc=f"[REDACTED]@{host}").geturl()


def url_identity(url: str) -> str:
    """Key that collapses scheme, `www.`, trailing-slash, double-slash and `.md` variants.

    Used only to flag likely duplicates for the reviewer. Nothing is merged or dropped.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path
    while "//" in path:
        path = path.replace("//", "/")
    path = path.rstrip("/")
    # Docs sites (docs.ollama.com in both live runs) serve the same page at `/x` and `/x.md`.
    if path.endswith(".md"):
        path = path[:-3]
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")
