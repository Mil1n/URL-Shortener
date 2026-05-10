"""Lightweight modern URL shortener prototype (single-file, SQLite).

Run:
    python src/server.py

Optional env:
    SHORTENER_DB_PATH=shortener.db
    SHORTENER_API_KEY=dev-secret-key
"""

from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import string
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import make_server

DB_PATH = os.getenv("SHORTENER_DB_PATH", "shortener.db")
API_KEY = os.getenv("SHORTENER_API_KEY", "dev-secret-key")


BOT_MARKERS = ("bot", "spider", "crawler", "headless", "preview")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
        """
    )
    conn.commit()
    conn.close()


def json_response(start_response, status: HTTPStatus, payload: str):
    body = payload.encode("utf-8")
    start_response(f"{status.value} {status.phrase}", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]


def make_slug(n: int = 7) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(n))


def require_api_key(environ) -> bool:
    return environ.get("HTTP_X_API_KEY") == API_KEY


def parse_json_body(environ) -> dict:
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    raw = environ["wsgi.input"].read(size).decode("utf-8") if size else ""
    if not raw:
        return {}
    import json

    return json.loads(raw)


def is_bot(ua: str | None) -> bool:
    if not ua:
        return False
    lu = ua.lower()
    return any(marker in lu for marker in BOT_MARKERS)


def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path == "/health":
        return json_response(start_response, HTTPStatus.OK, '{"status":"ok"}')

    if method == "POST" and path == "/api/links":
        if not require_api_key(environ):
            return json_response(start_response, HTTPStatus.UNAUTHORIZED, '{"error":"invalid_api_key"}')
        try:
            body = parse_json_body(environ)
            destination = (body.get("destination_url") or "").strip()
            slug = (body.get("slug") or "").strip()
            expires_at = (body.get("expires_at") or "").strip() or None
            if not destination or not urlparse(destination).scheme:
                return json_response(start_response, HTTPStatus.BAD_REQUEST, '{"error":"invalid_destination_url"}')
            if not slug:
                slug = make_slug()

            conn = db()
            conn.execute(
                "INSERT INTO links(slug, destination_url, created_at, expires_at, is_active) VALUES(?,?,?,?,1)",
                (slug, destination, now_iso(), expires_at),
            )
            conn.commit()
            conn.close()
            import json

            return json_response(start_response, HTTPStatus.CREATED, json.dumps({"slug": slug, "short_url": f"/{slug}"}))
        except sqlite3.IntegrityError:
            return json_response(start_response, HTTPStatus.CONFLICT, '{"error":"slug_taken"}')
        except Exception:
            return json_response(start_response, HTTPStatus.BAD_REQUEST, '{"error":"invalid_payload"}')

    if method == "GET" and path.startswith("/api/links/") and path.endswith("/stats"):
        if not require_api_key(environ):
            return json_response(start_response, HTTPStatus.UNAUTHORIZED, '{"error":"invalid_api_key"}')
        slug = path.split("/")[3]
        conn = db()
        row = conn.execute("SELECT id FROM links WHERE slug=?", (slug,)).fetchone()
        if not row:
            conn.close()
            return json_response(start_response, HTTPStatus.NOT_FOUND, '{"error":"not_found"}')
        link_id = row["id"]
        total = conn.execute("SELECT COUNT(*) AS c FROM clicks WHERE link_id=?", (link_id,)).fetchone()["c"]
        unique = conn.execute("SELECT COUNT(DISTINCT ip_hash) AS c FROM clicks WHERE link_id=?", (link_id,)).fetchone()["c"]
        bots = conn.execute("SELECT COUNT(*) AS c FROM clicks WHERE link_id=? AND is_bot=1", (link_id,)).fetchone()["c"]
        conn.close()
        import json

        return json_response(start_response, HTTPStatus.OK, json.dumps({"slug": slug, "total_clicks": total, "unique_clicks": unique, "bot_clicks": bots}))

    if method == "GET" and path.count("/") == 1 and len(path) > 1:
        slug = path[1:]
        conn = db()
        link = conn.execute("SELECT id, destination_url, expires_at, is_active FROM links WHERE slug=?", (slug,)).fetchone()
        if not link:
            conn.close()
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Not found"]
        if not link["is_active"]:
            conn.close()
            start_response("410 Gone", [("Content-Type", "text/plain")])
            return [b"Link disabled"]
        if link["expires_at"] and datetime.fromisoformat(link["expires_at"]).replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            conn.close()
            start_response("410 Gone", [("Content-Type", "text/plain")])
            return [b"Link expired"]

        ip = environ.get("REMOTE_ADDR", "0.0.0.0")
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

    return json_response(start_response, HTTPStatus.NOT_FOUND, '{"error":"route_not_found"}')


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8080"))
    print(f"URL Shortener started on http://127.0.0.1:{port}")
    with make_server("0.0.0.0", port, app) as server:
        server.serve_forever()
