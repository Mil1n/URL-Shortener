"""Click enrichment helpers."""

from __future__ import annotations

BOT_MARKERS = ("bot", "spider", "crawler", "headless", "preview")


def is_bot(ua: str | None) -> bool:
    if not ua:
        return False
    lu = ua.lower()
    return any(marker in lu for marker in BOT_MARKERS)


def classify_user_agent(ua: str | None) -> dict[str, str]:
    value = (ua or "").lower()
    device = "mobile" if any(token in value for token in ("mobile", "iphone", "android")) else "desktop"
    if "ipad" in value or "tablet" in value:
        device = "tablet"
    browser = "unknown"
    for marker, name in (("edg/", "edge"), ("chrome/", "chrome"), ("firefox/", "firefox"), ("safari/", "safari")):
        if marker in value:
            browser = name
            break
    os_name = "unknown"
    for marker, name in (("windows", "windows"), ("mac os", "macos"), ("iphone", "ios"), ("ipad", "ios"), ("android", "android"), ("linux", "linux")):
        if marker in value:
            os_name = name
            break
    return {"device": device, "browser": browser, "os": os_name}


def country_from_environ(environ) -> str:
    return (environ.get("HTTP_CF_IPCOUNTRY") or environ.get("HTTP_X_COUNTRY") or "unknown").upper()
