"""Validation and safety helpers for slugs and destination URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
RESERVED_SLUGS = {"api", "health", "admin", "stats", "preview", "qr", "assets", "favicon.ico"}


def validate_slug(slug: str) -> str | None:
    if not SLUG_RE.match(slug):
        return "slug_must_match_[A-Za-z0-9_-]{3,64}"
    if slug.lower() in RESERVED_SLUGS:
        return "slug_reserved"
    return None


def is_private_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.lower() in {"localhost"} or hostname.endswith(".local")
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved


def safety_status_for_url(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    suspicious_markers = ("xn--", "phish", "malware", "login-secure", "verify-account")
    return "suspicious" if any(marker in hostname for marker in suspicious_markers) else "unchecked"


def validate_destination_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "destination_url_must_be_http_or_https"
    if parsed.username or parsed.password:
        return "destination_url_credentials_not_allowed"
    if is_private_host(parsed.hostname):
        return "destination_url_private_hosts_not_allowed"
    return None
