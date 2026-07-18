"""Time helpers shared by the WSGI application."""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
