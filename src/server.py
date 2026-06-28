"""Lightweight modern URL shortener prototype (single-file, SQLite).

Run:
    python src/server.py

Optional env:
    SHORTENER_DB_PATH=shortener.db
    SHORTENER_API_KEY=dev-secret-key
    SHORTENER_BASE_URL=http://127.0.0.1:8080
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import os
import random
import re
import sqlite3
import string
import time
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from wsgiref.simple_server import make_server

DB_PATH = os.getenv("SHORTENER_DB_PATH", "shortener.db")
API_KEY = os.getenv("SHORTENER_API_KEY", "dev-secret-key")
BASE_URL = os.getenv("SHORTENER_BASE_URL", "").rstrip("/")

BOT_MARKERS = ("bot", "spider", "crawler", "headless", "preview")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
RESERVED_SLUGS = {"api", "health", "admin", "stats", "preview", "qr", "assets", "favicon.ico"}
MAX_BODY_BYTES = 1_000_000
CREATE_RATE_LIMIT = 60
REDIRECT_RATE_LIMIT = 600
RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMITS: dict[tuple[str, str], list[float]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            destination_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            user_agent TEXT,
            referrer TEXT,
            is_bot INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(link_id) REFERENCES links(id)
        );

        CREATE INDEX IF NOT EXISTS idx_clicks_link_id ON clicks(link_id);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_bot ON clicks(link_id, is_bot);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_ip ON clicks(link_id, ip_hash);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_ts ON clicks(link_id, ts);
        """
    )
    conn.commit()
    conn.close()


def json_response(start_response, status: HTTPStatus, payload: dict | list):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(
        f"{status.value} {status.phrase}",
        [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))],
    )
    return [body]


def text_response(start_response, status: HTTPStatus, payload: str):
    body = payload.encode("utf-8")
    start_response(f"{status.value} {status.phrase}", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]


def make_slug(n: int = 7) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(chars) for _ in range(n))


def require_api_key(environ) -> bool:
    return environ.get("HTTP_X_API_KEY") == API_KEY


def client_ip(environ) -> str:
    forwarded_for = environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return forwarded_for or environ.get("REMOTE_ADDR", "0.0.0.0")


def rate_limited(scope: str, key: str, limit: int) -> bool:
    now = time.monotonic()
    bucket_key = (scope, key)
    bucket = [ts for ts in _RATE_LIMITS.get(bucket_key, []) if now - ts < RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= limit:
        _RATE_LIMITS[bucket_key] = bucket
        return True
    bucket.append(now)
    _RATE_LIMITS[bucket_key] = bucket
    return False


def parse_json_body(environ) -> dict:
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    if size > MAX_BODY_BYTES:
        raise ValueError("request_body_too_large")
    raw = environ["wsgi.input"].read(size).decode("utf-8") if size else ""
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("json_object_required")
    return payload


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
        return hostname in {"localhost"}
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast


def validate_destination_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "destination_url_must_be_http_or_https"
    if is_private_host(parsed.hostname):
        return "destination_url_private_hosts_not_allowed"
    return None


def add_utm_params(url: str, payload: dict) -> str:
    utm = {key: str(payload[key]).strip() for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term") if payload.get(key)}
    if not utm:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(utm)
    return urlunparse(parsed._replace(query=urlencode(query)))


def is_bot(ua: str | None) -> bool:
    if not ua:
        return False
    lu = ua.lower()
    return any(marker in lu for marker in BOT_MARKERS)


def public_short_url(environ, slug: str) -> str:
    if BASE_URL:
        return f"{BASE_URL}/{slug}"
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "127.0.0.1")
    return f"{scheme}://{host}/{slug}"


def link_payload(environ, row: sqlite3.Row) -> dict:
    return {
        "slug": row["slug"],
        "destination_url": row["destination_url"],
        "short_url": public_short_url(environ, row["slug"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "is_active": bool(row["is_active"]),
    }


def qr_svg(data: str) -> str:
    digest = hashlib.sha256(data.encode("utf-8")).digest()
    size = 21
    cell = 8
    quiet = 4
    modules = [[False for _ in range(size)] for _ in range(size)]

    def finder(x: int, y: int) -> None:
        for dy in range(7):
            for dx in range(7):
                border = dx in {0, 6} or dy in {0, 6}
                center = 2 <= dx <= 4 and 2 <= dy <= 4
                modules[y + dy][x + dx] = border or center

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)
    bit_index = 0
    for y in range(size):
        for x in range(size):
            if modules[y][x] or (x < 7 and y < 7) or (x >= size - 7 and y < 7) or (x < 7 and y >= size - 7):
                continue
            byte = digest[(bit_index // 8) % len(digest)]
            modules[y][x] = bool(byte & (1 << (bit_index % 8)))
            bit_index += 1
    width = (size + quiet * 2) * cell
    rects = []
    for y, row in enumerate(modules):
        for x, filled in enumerate(row):
            if filled:
                rects.append(f'<rect x="{(x + quiet) * cell}" y="{(y + quiet) * cell}" width="{cell}" height="{cell}"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{width}" viewBox="0 0 {width} {width}"><rect width="100%" height="100%" fill="white"/><g fill="black">{"".join(rects)}</g></svg>'


def create_link(environ, start_response, payload: dict):
    destination = add_utm_params((payload.get("destination_url") or "").strip(), payload)
    slug = (payload.get("slug") or "").strip() or make_slug()
    expires_raw = (payload.get("expires_at") or "").strip() or None

    url_error = validate_destination_url(destination)
    if url_error:
        return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": url_error})
    slug_error = validate_slug(slug)
    if slug_error:
        return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": slug_error})
    try:
        expires_at = parse_iso_datetime(expires_raw)
    except ValueError:
        return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_expires_at"})

    conn = db()
    try:
        conn.execute(
            "INSERT INTO links(slug, destination_url, created_at, expires_at, is_active) VALUES(?,?,?,?,1)",
            (slug, destination, now_iso(), expires_at),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return json_response(start_response, HTTPStatus.CONFLICT, {"error": "slug_taken"})
    conn.close()
    return json_response(start_response, HTTPStatus.CREATED, link_payload(environ, row))


def app(environ, start_response):
    init_db()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path == "/health":
        return json_response(start_response, HTTPStatus.OK, {"status": "ok"})

    if path == "/api/links" and method in {"GET", "POST"}:
        if not require_api_key(environ):
            return json_response(start_response, HTTPStatus.UNAUTHORIZED, {"error": "invalid_api_key"})
        if rate_limited("api", client_ip(environ), CREATE_RATE_LIMIT):
            return json_response(start_response, HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
        if method == "GET":
            conn = db()
            rows = conn.execute("SELECT * FROM links ORDER BY id DESC LIMIT 100").fetchall()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, {"links": [link_payload(environ, row) for row in rows]})
        try:
            body = parse_json_body(environ)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
        return create_link(environ, start_response, body)

    if path == "/api/links/import" and method == "POST":
        if not require_api_key(environ):
            return json_response(start_response, HTTPStatus.UNAUTHORIZED, {"error": "invalid_api_key"})
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(size).decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(raw)))
        except (ValueError, csv.Error, UnicodeDecodeError):
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_csv"})
        created = []
        errors = []
        for index, row in enumerate(rows, start=2):
            capture = {}

            def local_start(status, headers):
                capture["status"] = status
                capture["headers"] = headers

            result = b"".join(create_link(environ, local_start, row)).decode("utf-8")
            if capture.get("status", "").startswith("201"):
                created.append(json.loads(result))
            else:
                errors.append({"line": index, "detail": json.loads(result)})
        return json_response(start_response, HTTPStatus.CREATED if created else HTTPStatus.BAD_REQUEST, {"created": created, "errors": errors})

    if path.startswith("/api/links/"):
        if not require_api_key(environ):
            return json_response(start_response, HTTPStatus.UNAUTHORIZED, {"error": "invalid_api_key"})
        parts = path.strip("/").split("/")
        if len(parts) >= 3:
            slug = parts[2]
        else:
            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
        conn = db()
        row = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
        if not row:
            conn.close()
            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        if len(parts) == 4 and parts[3] == "stats" and method == "GET":
            link_id = row["id"]
            total = conn.execute("SELECT COUNT(*) AS c FROM clicks WHERE link_id=?", (link_id,)).fetchone()["c"]
            unique = conn.execute("SELECT COUNT(DISTINCT ip_hash) AS c FROM clicks WHERE link_id=?", (link_id,)).fetchone()["c"]
            bots = conn.execute("SELECT COUNT(*) AS c FROM clicks WHERE link_id=? AND is_bot=1", (link_id,)).fetchone()["c"]
            referrers = conn.execute("SELECT COALESCE(NULLIF(referrer,''),'direct') AS referrer, COUNT(*) AS clicks FROM clicks WHERE link_id=? GROUP BY 1 ORDER BY clicks DESC LIMIT 10", (link_id,)).fetchall()
            daily = conn.execute("SELECT substr(ts,1,10) AS date, COUNT(*) AS clicks FROM clicks WHERE link_id=? GROUP BY 1 ORDER BY date", (link_id,)).fetchall()
            recent = conn.execute("SELECT ts, referrer, user_agent, is_bot FROM clicks WHERE link_id=? ORDER BY id DESC LIMIT 10", (link_id,)).fetchall()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, {
                "slug": slug,
                "total_clicks": total,
                "unique_clicks": unique,
                "bot_clicks": bots,
                "bot_ratio": round(bots / total, 4) if total else 0,
                "top_referrers": [dict(item) for item in referrers],
                "clicks_by_day": [dict(item) for item in daily],
                "recent_clicks": [{"ts": item["ts"], "referrer": item["referrer"] or "direct", "user_agent": item["user_agent"], "is_bot": bool(item["is_bot"])} for item in recent],
            })
        if len(parts) == 3 and method == "PATCH":
            try:
                body = parse_json_body(environ)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                conn.close()
                return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
            updates = []
            values = []
            if "destination_url" in body:
                destination = add_utm_params(str(body.get("destination_url") or "").strip(), body)
                url_error = validate_destination_url(destination)
                if url_error:
                    conn.close()
                    return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": url_error})
                updates.append("destination_url=?")
                values.append(destination)
            if "expires_at" in body:
                try:
                    expires_at = parse_iso_datetime((body.get("expires_at") or "").strip() or None)
                except ValueError:
                    conn.close()
                    return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_expires_at"})
                updates.append("expires_at=?")
                values.append(expires_at)
            if "is_active" in body:
                updates.append("is_active=?")
                values.append(1 if bool(body.get("is_active")) else 0)
            if not updates:
                conn.close()
                return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "no_fields_to_update"})
            values.append(slug)
            conn.execute(f"UPDATE links SET {', '.join(updates)} WHERE slug=?", values)
            conn.commit()
            updated = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, link_payload(environ, updated))
        if len(parts) == 3 and method == "DELETE":
            conn.execute("UPDATE links SET is_active=0 WHERE slug=?", (slug,))
            conn.commit()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, {"slug": slug, "is_active": False})
        conn.close()
        return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "route_not_found"})

    if method == "GET" and path.startswith("/qr/") and path.count("/") == 2:
        slug = path.split("/")[2]
        conn = db()
        row = conn.execute("SELECT slug FROM links WHERE slug=?", (slug,)).fetchone()
        conn.close()
        if not row:
            return text_response(start_response, HTTPStatus.NOT_FOUND, "Not found")
        body = qr_svg(public_short_url(environ, slug)).encode("utf-8")
        start_response("200 OK", [("Content-Type", "image/svg+xml; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]

    if method == "GET" and path.startswith("/preview/") and path.count("/") == 2:
        slug = path.split("/")[2]
        conn = db()
        row = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
        conn.close()
        if not row:
            return text_response(start_response, HTTPStatus.NOT_FOUND, "Not found")
        return json_response(start_response, HTTPStatus.OK, link_payload(environ, row))

    if method == "GET" and path.count("/") == 1 and len(path) > 1:
        slug = path[1:]
        if rate_limited("redirect", f"{slug}:{client_ip(environ)}", REDIRECT_RATE_LIMIT):
            return text_response(start_response, HTTPStatus.TOO_MANY_REQUESTS, "Rate limited")
        conn = db()
        link = conn.execute("SELECT id, destination_url, expires_at, is_active FROM links WHERE slug=?", (slug,)).fetchone()
        if not link:
            conn.close()
            return text_response(start_response, HTTPStatus.NOT_FOUND, "Not found")
        if not link["is_active"]:
            conn.close()
            return text_response(start_response, HTTPStatus.GONE, "Link disabled")
        if is_expired(link["expires_at"]):
            conn.close()
            return text_response(start_response, HTTPStatus.GONE, "Link expired")

        ip = client_ip(environ)
        ua = environ.get("HTTP_USER_AGENT", "")
        ref = environ.get("HTTP_REFERER", "")
        ip_hash = hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:16]
        bot = 1 if is_bot(ua) else 0
        conn.execute(
            "INSERT INTO clicks(link_id, ts, ip_hash, user_agent, referrer, is_bot) VALUES(?,?,?,?,?,?)",
            (link["id"], now_iso(), ip_hash, ua, ref, bot),
        )
        conn.commit()
        conn.close()

        start_response("302 Found", [("Location", link["destination_url"])])
        return [b""]

    return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "route_not_found"})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8080"))
    print(f"URL Shortener started on http://127.0.0.1:{port}")
    with make_server("0.0.0.0", port, app) as server:
        server.serve_forever()
