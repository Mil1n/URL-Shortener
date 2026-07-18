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
import hmac
import html
import io
import json
import os
import random
import secrets
import sqlite3
import string
import urllib.error
import urllib.request
from http import HTTPStatus
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from src.analytics import classify_user_agent, country_from_environ, is_bot
from src.ratelimit import InMemoryRateLimiter
from src.share import share_svg
from src.time_utils import is_expired, now_iso, parse_iso_datetime
from src.validation import RESERVED_SLUGS, SLUG_RE, safety_status_for_url, validate_destination_url, validate_slug
from wsgiref.simple_server import make_server


DB_PATH = os.getenv("SHORTENER_DB_PATH", "shortener.db")
API_KEY = os.getenv("SHORTENER_API_KEY", "dev-secret-key")
BASE_URL = os.getenv("SHORTENER_BASE_URL", "").rstrip("/")
DEFAULT_WORKSPACE_ID = os.getenv("SHORTENER_WORKSPACE_ID", "default")
WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("SHORTENER_WEBHOOK_TIMEOUT", "2"))

MAX_BODY_BYTES = 1_000_000
CREATE_RATE_LIMIT = 60
REDIRECT_RATE_LIMIT = 600
RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMITER = InMemoryRateLimiter(RATE_LIMIT_WINDOW_SECONDS)
_RATE_LIMITS = _RATE_LIMITER.buckets


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            scopes TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            last_used_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );

        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            destination_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            owner_key_id INTEGER,
            safety_status TEXT NOT NULL DEFAULT 'unchecked'
        );

        CREATE TABLE IF NOT EXISTS link_destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            destination_url TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 100,
            created_at TEXT NOT NULL,
            FOREIGN KEY(link_id) REFERENCES links(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            user_agent TEXT,
            referrer TEXT,
            is_bot INTEGER NOT NULL DEFAULT 0,
            device TEXT NOT NULL DEFAULT 'unknown',
            browser TEXT NOT NULL DEFAULT 'unknown',
            os TEXT NOT NULL DEFAULT 'unknown',
            country TEXT NOT NULL DEFAULT 'unknown',
            variant_label TEXT,
            FOREIGN KEY(link_id) REFERENCES links(id)
        );

        CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            url TEXT NOT NULL,
            events TEXT NOT NULL,
            secret TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        );

        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id INTEGER NOT NULL,
            event TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            response_code INTEGER,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            FOREIGN KEY(webhook_id) REFERENCES webhooks(id)
        );

        CREATE INDEX IF NOT EXISTS idx_links_workspace ON links(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_id ON clicks(link_id);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_bot ON clicks(link_id, is_bot);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_ip ON clicks(link_id, ip_hash);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_ts ON clicks(link_id, ts);
        CREATE INDEX IF NOT EXISTS idx_clicks_link_variant ON clicks(link_id, variant_label);
        """
    )
    conn.execute("INSERT OR IGNORE INTO workspaces(id, name, created_at) VALUES(?,?,?)", (DEFAULT_WORKSPACE_ID, "Default", now_iso()))
    for table, additions in {
        "links": [("workspace_id", "TEXT NOT NULL DEFAULT 'default'"), ("owner_key_id", "INTEGER"), ("safety_status", "TEXT NOT NULL DEFAULT 'unchecked'")],
        "clicks": [("device", "TEXT NOT NULL DEFAULT 'unknown'"), ("browser", "TEXT NOT NULL DEFAULT 'unknown'"), ("os", "TEXT NOT NULL DEFAULT 'unknown'"), ("country", "TEXT NOT NULL DEFAULT 'unknown'"), ("variant_label", "TEXT")],
        "webhook_deliveries": [("payload", "TEXT NOT NULL DEFAULT '{}'"), ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "TEXT"), ("delivered_at", "TEXT")],
    }.items():
        for column, definition in additions:
            ensure_column(conn, table, column, definition)
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)", (1, now_iso()))
    conn.commit()
    conn.close()


def json_response(start_response, status: HTTPStatus, payload: dict | list):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(f"{status.value} {status.phrase}", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))])
    return [body]


def text_response(start_response, status: HTTPStatus, payload: str, content_type: str = "text/plain; charset=utf-8"):
    body = payload.encode("utf-8")
    start_response(f"{status.value} {status.phrase}", [("Content-Type", content_type), ("Content-Length", str(len(body)))])
    return [body]


def make_slug(n: int = 7) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(chars) for _ in range(n))


def hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scopes_from_row(row: sqlite3.Row | None) -> set[str]:
    if not row:
        return {"*"}
    return {scope.strip() for scope in row["scopes"].split(",") if scope.strip()}


def api_context(environ) -> dict | None:
    raw = environ.get("HTTP_X_API_KEY", "")
    if not raw:
        return None
    if raw == API_KEY:
        return {"workspace_id": DEFAULT_WORKSPACE_ID, "key_id": None, "scopes": {"*"}, "legacy": True}
    conn = db()
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (hash_key(raw),)).fetchone()
    if not row:
        conn.close()
        return None
    if row["expires_at"] and is_expired(row["expires_at"]):
        conn.close()
        return None
    conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    conn.commit()
    conn.close()
    return {"workspace_id": row["workspace_id"], "key_id": row["id"], "scopes": scopes_from_row(row), "legacy": False}


def require_api_key(environ, scope: str | None = None) -> dict | None:
    context = api_context(environ)
    if not context:
        return None
    scopes = context["scopes"]
    if scope and "*" not in scopes and scope not in scopes:
        return None
    return context


def auth_error(environ, scope: str | None = None) -> HTTPStatus:
    context = api_context(environ)
    if not context:
        return HTTPStatus.UNAUTHORIZED
    scopes = context["scopes"]
    if scope and "*" not in scopes and scope not in scopes:
        return HTTPStatus.FORBIDDEN
    return HTTPStatus.UNAUTHORIZED


def client_ip(environ) -> str:
    forwarded_for = environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
    return forwarded_for or environ.get("REMOTE_ADDR", "0.0.0.0")


def rate_limited(scope: str, key: str, limit: int) -> bool:
    return _RATE_LIMITER.is_limited(scope, key, limit)


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


def add_utm_params(url: str, payload: dict) -> str:
    utm = {key: str(payload[key]).strip() for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term") if payload.get(key)}
    if not utm:
        return url
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(utm)
    return urlunparse(parsed._replace(query=urlencode(query)))


def public_short_url(environ, slug: str) -> str:
    if BASE_URL:
        return f"{BASE_URL}/{slug}"
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "127.0.0.1")
    return f"{scheme}://{host}/{slug}"


def row_destinations(conn: sqlite3.Connection, link_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT label, destination_url, weight FROM link_destinations WHERE link_id=? ORDER BY id", (link_id,)).fetchall()]


def link_payload(environ, row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict:
    own_conn = conn or db()
    destinations = row_destinations(own_conn, row["id"])
    if conn is None:
        own_conn.close()
    payload = {
        "slug": row["slug"],
        "destination_url": row["destination_url"],
        "short_url": public_short_url(environ, row["slug"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "is_active": bool(row["is_active"]),
        "workspace_id": row["workspace_id"],
        "safety_status": row["safety_status"],
    }
    if destinations:
        payload["destinations"] = destinations
    return payload


def normalized_destinations(payload: dict, primary_url: str) -> list[dict]:
    raw = payload.get("destinations")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("destinations_must_be_non_empty_list")
    result = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("destination_item_must_be_object")
        url = add_utm_params(str(item.get("url") or item.get("destination_url") or "").strip(), payload)
        error = validate_destination_url(url)
        if error:
            raise ValueError(error)
        weight = int(item.get("weight", 100))
        if weight <= 0 or weight > 10000:
            raise ValueError("destination_weight_must_be_1_10000")
        result.append({"label": str(item.get("label") or f"variant-{index}").strip()[:64], "destination_url": url, "weight": weight})
    if not any(item["destination_url"] == primary_url for item in result):
        result.insert(0, {"label": "control", "destination_url": primary_url, "weight": 100})
    return result


def create_link(environ, start_response, payload: dict, context: dict):
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
        destinations = normalized_destinations(payload, destination)
    except (ValueError, TypeError) as exc:
        return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": str(exc) or "invalid_payload"})
    conn = db()
    try:
        safety = safety_status_for_url(destination)
        conn.execute(
            "INSERT INTO links(slug, destination_url, created_at, expires_at, is_active, workspace_id, owner_key_id, safety_status) VALUES(?,?,?,?,1,?,?,?)",
            (slug, destination, now_iso(), expires_at, context["workspace_id"], context["key_id"], safety),
        )
        link_id = conn.execute("SELECT id FROM links WHERE slug=?", (slug,)).fetchone()["id"]
        for item in destinations:
            conn.execute(
                "INSERT INTO link_destinations(link_id, label, destination_url, weight, created_at) VALUES(?,?,?,?,?)",
                (link_id, item["label"], item["destination_url"], item["weight"], now_iso()),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return json_response(start_response, HTTPStatus.CONFLICT, {"error": "slug_taken"})
    response = link_payload(environ, row, conn)
    conn.close()
    return json_response(start_response, HTTPStatus.CREATED, response)


def choose_destination(conn: sqlite3.Connection, link: sqlite3.Row, visitor_key: str) -> tuple[str, str | None]:
    variants = conn.execute("SELECT label, destination_url, weight FROM link_destinations WHERE link_id=?", (link["id"],)).fetchall()
    if not variants:
        return link["destination_url"], None
    total = sum(max(1, row["weight"]) for row in variants)
    seed = hashlib.sha256(f"{link['id']}|{visitor_key}".encode("utf-8")).hexdigest()
    pick = (int(seed, 16) % total) + 1
    upto = 0
    for row in variants:
        upto += max(1, row["weight"])
        if pick <= upto:
            return row["destination_url"], row["label"]
    last = variants[-1]
    return last["destination_url"], last["label"]


def enqueue_webhook_events(conn: sqlite3.Connection, workspace_id: str, event: str, payload: dict) -> int:
    hooks = conn.execute("SELECT * FROM webhooks WHERE workspace_id=? AND is_active=1", (workspace_id,)).fetchall()
    body = json.dumps({"event": event, "payload": payload}, ensure_ascii=False, separators=(",", ":"))
    queued = 0
    for hook in hooks:
        events = {item.strip() for item in hook["events"].split(",")}
        if event not in events and "*" not in events:
            continue
        conn.execute(
            "INSERT INTO webhook_deliveries(webhook_id, event, payload, status, next_attempt_at, created_at) VALUES(?,?,?,?,?,?)",
            (hook["id"], event, body, "pending", now_iso(), now_iso()),
        )
        queued += 1
    return queued


def process_webhook_deliveries(conn: sqlite3.Connection, limit: int = 20) -> dict:
    rows = conn.execute(
        """
        SELECT d.*, w.url, w.secret
        FROM webhook_deliveries d
        JOIN webhooks w ON w.id=d.webhook_id
        WHERE d.status IN ('pending','failed')
          AND (d.next_attempt_at IS NULL OR d.next_attempt_at<=?)
          AND d.attempts < 3
        ORDER BY d.id
        LIMIT ?
        """,
        (now_iso(), limit),
    ).fetchall()
    delivered = 0
    failed = 0
    for row in rows:
        body = row["payload"].encode("utf-8")
        signature = hmac.new(row["secret"].encode("utf-8"), body, hashlib.sha256).hexdigest()
        request = urllib.request.Request(row["url"], data=body, method="POST", headers={"Content-Type": "application/json", "X-Shortener-Signature": signature})
        status = "delivered"
        code = None
        error = None
        delivered_at = now_iso()
        try:
            with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            status = "failed"
            code = exc.code
            error = str(exc)
            delivered_at = None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status = "failed"
            error = str(exc)
            delivered_at = None
        attempts = row["attempts"] + 1
        next_attempt_at = None if status == "delivered" or attempts >= 3 else now_iso()
        conn.execute(
            "UPDATE webhook_deliveries SET status=?, response_code=?, error=?, attempts=?, next_attempt_at=?, delivered_at=? WHERE id=?",
            (status, code, error, attempts, next_attempt_at, delivered_at, row["id"]),
        )
        delivered += 1 if status == "delivered" else 0
        failed += 1 if status == "failed" else 0
    return {"processed": len(rows), "delivered": delivered, "failed": failed}

def dashboard(environ, start_response):
    if not require_api_key(environ, "links:read"):
        return json_response(start_response, auth_error(environ, "links:read"), {"error": "invalid_api_key"})
    conn = db()
    rows = conn.execute("SELECT slug, destination_url, created_at, is_active, safety_status FROM links ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    items = "".join(
        f"<tr><td><a href='/{html.escape(row['slug'])}'>{html.escape(row['slug'])}</a></td><td>{html.escape(row['destination_url'])}</td><td>{html.escape(row['created_at'])}</td><td>{'active' if row['is_active'] else 'disabled'}</td><td>{html.escape(row['safety_status'])}</td></tr>"
        for row in rows
    )
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>URL Shortener Admin</title><style>body{{font-family:system-ui;margin:2rem}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.6rem;text-align:left}}</style></head><body><h1>Debug link listing</h1><p>Latest 50 links for quick inspection; this is not a full admin dashboard yet.</p><table><thead><tr><th>Slug</th><th>Destination</th><th>Created</th><th>Status</th><th>Safety</th></tr></thead><tbody>{items}</tbody></table></body></html>"""
    return text_response(start_response, HTTPStatus.OK, page, "text/html; charset=utf-8")


def app(environ, start_response):
    init_db()
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    query = parse_qs(environ.get("QUERY_STRING", ""))

    if method == "GET" and path == "/health":
        return json_response(start_response, HTTPStatus.OK, {"status": "ok"})
    if method == "GET" and path == "/admin":
        return dashboard(environ, start_response)

    if path == "/api/keys" and method in {"GET", "POST"}:
        scope = "keys:write" if method == "POST" else "keys:read"
        context = require_api_key(environ, scope)
        if not context:
            return json_response(start_response, auth_error(environ, scope), {"error": "invalid_api_key"})
        conn = db()
        if method == "GET":
            rows = conn.execute("SELECT id, workspace_id, name, scopes, created_at, expires_at, revoked_at, last_used_at FROM api_keys WHERE workspace_id=? ORDER BY id DESC", (context["workspace_id"],)).fetchall()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, {"api_keys": [dict(row) for row in rows]})
        try:
            body = parse_json_body(environ)
            expires_at = parse_iso_datetime((body.get("expires_at") or "").strip() or None)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            conn.close()
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
        raw_key = "sk_" + secrets.token_urlsafe(32)
        requested_scopes = body.get("scopes") or ["links:read", "links:write", "stats:read"]
        allowed_scopes = {"links:read", "links:write", "stats:read", "keys:read", "keys:write", "webhooks:read", "webhooks:write"}
        if not isinstance(requested_scopes, list) or any(scope not in allowed_scopes for scope in requested_scopes):
            conn.close()
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_scopes"})
        scopes = ",".join(requested_scopes)
        conn.execute("INSERT INTO api_keys(workspace_id, name, key_hash, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?)", (context["workspace_id"], str(body.get("name") or "API key"), hash_key(raw_key), scopes, now_iso(), expires_at))
        conn.commit()
        key_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.close()
        return json_response(start_response, HTTPStatus.CREATED, {"id": key_id, "api_key": raw_key, "scopes": scopes.split(",")})

    if path == "/api/webhooks/deliveries/process" and method == "POST":
        context = require_api_key(environ, "webhooks:write")
        if not context:
            return json_response(start_response, auth_error(environ, "webhooks:write"), {"error": "invalid_api_key"})
        conn = db()
        result = process_webhook_deliveries(conn)
        conn.commit()
        conn.close()
        return json_response(start_response, HTTPStatus.OK, result)

    if path == "/api/webhooks" and method in {"GET", "POST"}:
        scope = "webhooks:write" if method == "POST" else "webhooks:read"
        context = require_api_key(environ, scope)
        if not context:
            return json_response(start_response, auth_error(environ, scope), {"error": "invalid_api_key"})
        conn = db()
        if method == "GET":
            rows = conn.execute("SELECT id, url, events, is_active, created_at FROM webhooks WHERE workspace_id=? ORDER BY id DESC", (context["workspace_id"],)).fetchall()
            conn.close()
            return json_response(start_response, HTTPStatus.OK, {"webhooks": [dict(row) for row in rows]})
        try:
            body = parse_json_body(environ)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            conn.close()
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
        url = str(body.get("url") or "").strip()
        url_error = validate_destination_url(url)
        if url_error:
            conn.close()
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": url_error})
        events = ",".join(body.get("events") or ["click.created"])
        secret = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO webhooks(workspace_id, url, events, secret, created_at) VALUES(?,?,?,?,?)", (context["workspace_id"], url, events, secret, now_iso()))
        conn.commit()
        hook_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.close()
        return json_response(start_response, HTTPStatus.CREATED, {"id": hook_id, "url": url, "events": events.split(","), "secret": secret})

    if path == "/api/links" and method in {"GET", "POST"}:
        scope = "links:write" if method == "POST" else "links:read"
        context = require_api_key(environ, scope)
        if not context:
            return json_response(start_response, auth_error(environ, scope), {"error": "invalid_api_key"})
        if rate_limited("api", client_ip(environ), CREATE_RATE_LIMIT):
            return json_response(start_response, HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
        if method == "GET":
            limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
            offset = max(int(query.get("offset", ["0"])[0]), 0)
            filters = ["workspace_id=?"]
            values: list[object] = [context["workspace_id"]]
            if query.get("q"):
                filters.append("(slug LIKE ? OR destination_url LIKE ?)")
                q = f"%{query['q'][0]}%"
                values.extend([q, q])
            if query.get("is_active"):
                filters.append("is_active=?")
                values.append(1 if query["is_active"][0].lower() in {"1", "true", "yes"} else 0)
            if query.get("created_from"):
                filters.append("created_at>=?")
                values.append(parse_iso_datetime(query["created_from"][0]))
            if query.get("created_to"):
                filters.append("created_at<=?")
                values.append(parse_iso_datetime(query["created_to"][0]))
            sort = "created_at ASC" if query.get("sort", ["desc"])[0] == "created_at" else "id DESC"
            conn = db()
            sql = f"SELECT * FROM links WHERE {' AND '.join(filters)} ORDER BY {sort} LIMIT ? OFFSET ?"
            rows = conn.execute(sql, (*values, limit, offset)).fetchall()
            total = conn.execute(f"SELECT COUNT(*) AS c FROM links WHERE {' AND '.join(filters)}", values).fetchone()["c"]
            payload = {"links": [link_payload(environ, row, conn) for row in rows], "pagination": {"limit": limit, "offset": offset, "total": total}}
            conn.close()
            return json_response(start_response, HTTPStatus.OK, payload)
        try:
            body = parse_json_body(environ)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return json_response(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
        return create_link(environ, start_response, body, context)

    if path == "/api/links/import" and method == "POST":
        context = require_api_key(environ, "links:write")
        if not context:
            return json_response(start_response, auth_error(environ, "links:write"), {"error": "invalid_api_key"})
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
            result = b"".join(create_link(environ, local_start, row, context)).decode("utf-8")
            if capture.get("status", "").startswith("201"):
                created.append(json.loads(result))
            else:
                errors.append({"line": index, "detail": json.loads(result)})
        return json_response(start_response, HTTPStatus.CREATED if created else HTTPStatus.BAD_REQUEST, {"created": created, "errors": errors})

    if path.startswith("/api/links/"):
        scope = "stats:read" if path.endswith("/stats") else "links:write"
        context = require_api_key(environ, scope)
        if not context:
            return json_response(start_response, auth_error(environ, scope), {"error": "invalid_api_key"})
        parts = path.strip("/").split("/")
        slug = parts[2] if len(parts) >= 3 else ""
        conn = db()
        row = conn.execute("SELECT * FROM links WHERE slug=? AND workspace_id=?", (slug, context["workspace_id"])).fetchone()
        if not row:
            conn.close()
            return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        if len(parts) == 4 and parts[3] == "stats" and method == "GET":
            link_id = row["id"]
            filters = ["link_id=?"]
            values: list[object] = [link_id]
            if query.get("date_from"):
                filters.append("ts>=?")
                values.append(parse_iso_datetime(query["date_from"][0]))
            if query.get("date_to"):
                filters.append("ts<=?")
                values.append(parse_iso_datetime(query["date_to"][0]))
            where = " AND ".join(filters)
            total = conn.execute(f"SELECT COUNT(*) AS c FROM clicks WHERE {where}", values).fetchone()["c"]
            unique = conn.execute(f"SELECT COUNT(DISTINCT ip_hash) AS c FROM clicks WHERE {where}", values).fetchone()["c"]
            bots = conn.execute(f"SELECT COUNT(*) AS c FROM clicks WHERE {where} AND is_bot=1", values).fetchone()["c"]
            def grouped(column: str, fallback: str = "unknown"):
                return [dict(item) for item in conn.execute(f"SELECT COALESCE(NULLIF({column},''),?) AS name, COUNT(*) AS clicks FROM clicks WHERE {where} GROUP BY 1 ORDER BY clicks DESC LIMIT 10", (fallback, *values)).fetchall()]
            referrers = conn.execute(f"SELECT COALESCE(NULLIF(referrer,''),'direct') AS referrer, COUNT(*) AS clicks FROM clicks WHERE {where} GROUP BY 1 ORDER BY clicks DESC LIMIT 10", values).fetchall()
            daily = conn.execute(f"SELECT substr(ts,1,10) AS date, COUNT(*) AS clicks FROM clicks WHERE {where} GROUP BY 1 ORDER BY date", values).fetchall()
            payload = {"slug": slug, "total_clicks": total, "unique_clicks": unique, "bot_clicks": bots, "bot_ratio": round(bots / total, 4) if total else 0, "top_referrers": [dict(item) for item in referrers], "clicks_by_day": [dict(item) for item in daily], "devices": grouped("device"), "browsers": grouped("browser"), "operating_systems": grouped("os"), "countries": grouped("country"), "variants": grouped("variant_label", "control")}
            if query.get("include_recent", ["false"])[0].lower() in {"1", "true", "yes"}:
                columns = "ts, referrer, is_bot, device, browser, os, country, variant_label"
                if query.get("include_user_agent", ["false"])[0].lower() in {"1", "true", "yes"}:
                    columns += ", user_agent"
                recent = conn.execute(f"SELECT {columns} FROM clicks WHERE {where} ORDER BY id DESC LIMIT 10", values).fetchall()
                payload["recent_clicks"] = [dict(item) for item in recent]
            conn.close()
            return json_response(start_response, HTTPStatus.OK, payload)
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
                updates.extend(["destination_url=?", "safety_status=?"])
                values.extend([destination, safety_status_for_url(destination)])
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
            payload = link_payload(environ, updated, conn)
            conn.close()
            return json_response(start_response, HTTPStatus.OK, payload)
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
        body = share_svg(public_short_url(environ, slug)).encode("utf-8")
        start_response("200 OK", [("Content-Type", "image/svg+xml; charset=utf-8"), ("Content-Length", str(len(body))), ("Content-Disposition", f'inline; filename="{slug}.svg"')])
        return [body]

    if method == "GET" and path.startswith("/preview/") and path.count("/") == 2:
        slug = path.split("/")[2]
        conn = db()
        row = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
        if not row:
            conn.close()
            return text_response(start_response, HTTPStatus.NOT_FOUND, "Not found")
        payload = link_payload(environ, row, conn)
        conn.close()
        return json_response(start_response, HTTPStatus.OK, payload)

    if method == "GET" and path.count("/") == 1 and len(path) > 1:
        slug = path[1:]
        if rate_limited("redirect", f"{slug}:{client_ip(environ)}", REDIRECT_RATE_LIMIT):
            return text_response(start_response, HTTPStatus.TOO_MANY_REQUESTS, "Rate limited")
        conn = db()
        link = conn.execute("SELECT * FROM links WHERE slug=?", (slug,)).fetchone()
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
        ua_info = classify_user_agent(ua)
        ip_hash = hashlib.sha256(f"{ip}|{ua}".encode("utf-8")).hexdigest()[:16]
        destination_url, variant_label = choose_destination(conn, link, ip_hash)
        bot = 1 if is_bot(ua) else 0
        conn.execute("INSERT INTO clicks(link_id, ts, ip_hash, user_agent, referrer, is_bot, device, browser, os, country, variant_label) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (link["id"], now_iso(), ip_hash, ua, ref, bot, ua_info["device"], ua_info["browser"], ua_info["os"], country_from_environ(environ), variant_label))
        enqueue_webhook_events(conn, link["workspace_id"], "click.created", {"slug": slug, "destination_url": destination_url, "variant_label": variant_label, "is_bot": bool(bot), "ts": now_iso()})
        conn.commit()
        conn.close()
        start_response("302 Found", [("Location", destination_url)])
        return [b""]

    return json_response(start_response, HTTPStatus.NOT_FOUND, {"error": "route_not_found"})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8080"))
    print(f"URL Shortener started on http://127.0.0.1:{port}")
    with make_server("0.0.0.0", port, app) as server:
        server.serve_forever()
