"""Uptime and self-reported health for the other apps in the media stack."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import core, events

SERVICE_INTERVAL = max(15, int(os.getenv("MG_SERVICE_INTERVAL", "60")))
REQUEST_TIMEOUT = 5
FAILURES_BEFORE_DOWN = 2

# path: an endpoint that answers without credentials (any reply below 500 counts as up).
# health: the app's own health-check API, read when an api_key is configured.
TYPES = {
    "jellyfin": {"path": "/health"},
    "emby": {"path": "/emby/System/Ping"},
    "plex": {"path": "/identity"},
    "sonarr": {"path": "/ping", "health": "/api/v3/health"},
    "radarr": {"path": "/ping", "health": "/api/v3/health"},
    "lidarr": {"path": "/ping", "health": "/api/v1/health"},
    "readarr": {"path": "/ping", "health": "/api/v1/health"},
    "prowlarr": {"path": "/ping", "health": "/api/v1/health"},
    "jellyseerr": {"path": "/api/v1/status"},
    "overseerr": {"path": "/api/v1/status"},
    "qbittorrent": {"path": "/api/v2/app/version"},
    "shrinkarr": {"path": "/api/health"},
    "generic": {"path": ""},
}
STATE = {"services": {}, "config_error": None, "config_mtime": None, "config": []}


def services_file():
    return Path(os.getenv("MG_SERVICES_FILE", str(core.BASE / "services.json")))


def config_source():
    """'MG_SERVICES' when the app list is set in the environment, else the services.json path."""
    return "MG_SERVICES" if os.getenv("MG_SERVICES", "").strip() else str(services_file())


def load_config():
    inline = os.getenv("MG_SERVICES", "").strip()
    if inline:
        version = ("env", inline)
    else:
        path = services_file()
        try:
            version = path.stat().st_mtime_ns
        except FileNotFoundError:
            STATE.update(config=[], config_error=None, config_mtime=None)
            return []
    if version == STATE["config_mtime"]:
        return STATE["config"]
    mtime = version
    try:
        raw = json.loads(inline or path.read_text())
        if not isinstance(raw, list):
            raise ValueError(f"{config_source()} must contain a list of services")
        config = []
        for item in raw:
            name, url = str(item.get("name", "")).strip(), str(item.get("url", "")).strip().rstrip("/")
            kind = str(item.get("type", "generic")).lower()
            if not name or not url.startswith(("http://", "https://")):
                raise ValueError(f"Each service needs a name and an http(s) url: {item!r:.80}")
            if kind not in TYPES:
                raise ValueError(f"Unknown type {kind!r} for {name}; use one of {', '.join(TYPES)}")
            path_map = item.get("path_map") or {}
            if not isinstance(path_map, dict) or not all(isinstance(v, str) for v in path_map.values()):
                raise ValueError(f"path_map for {name} must map app paths to media folders, e.g. {{\"/data/media\": \"\"}}")
            config.append({
                "name": name, "url": url, "type": kind, "api_key": item.get("api_key") or "",
                "path_map": {str(k).rstrip("/") or "/": v.strip("/") for k, v in path_map.items()},
            })
        STATE.update(config=config, config_error=None, config_mtime=mtime)
    except (OSError, ValueError, AttributeError) as exc:
        STATE.update(config_error=str(exc)[:300], config_mtime=mtime)
    return STATE["config"]


def _get(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": "MediaGuardian", **(headers or {})})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.status, response.read(256 * 1024)


def check(service):
    spec = TYPES[service["type"]]
    started = time.monotonic()
    result = {"up": False, "code": None, "error": None, "warnings": []}
    try:
        result["code"], _ = _get(service["url"] + spec["path"])
        result["up"] = True
    except urllib.error.HTTPError as exc:
        # 401/403/404 still proves the app is listening.
        result["code"] = exc.code
        result["up"] = exc.code < 500
        if not result["up"]:
            result["error"] = f"HTTP {exc.code}"
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        result["error"] = str(reason)[:200]
    result["ms"] = round((time.monotonic() - started) * 1000)
    if result["up"] and service["api_key"] and "health" in spec:
        try:
            _, body = _get(service["url"] + spec["health"], {"X-Api-Key": service["api_key"]})
            result["warnings"] = [
                f"{item.get('source', '')}: {item.get('message', '')}".strip(": ")
                for item in json.loads(body)
                if item.get("type") in ("warning", "error")
            ]
        except (OSError, ValueError, AttributeError) as exc:
            result["warnings"] = [f"Could not read health report: {getattr(exc, 'reason', exc)}"[:200]]
    return result


def display_url(url):
    parts = urllib.parse.urlsplit(url)
    # Drop any user:password@ and query string so credentials never reach the dashboard.
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def update(service, result, now):
    name = service["name"]
    previous = STATE["services"].get(name)
    failures = 0 if result["up"] else (previous["failures"] + 1 if previous else 1)
    was_down = bool(previous and previous["status"] == "down")
    status = "up" if result["up"] else ("down" if failures >= FAILURES_BEFORE_DOWN or was_down else "unstable")
    since = previous["since"] if previous and previous["status"] == status else now
    if status == "down" and not was_down:
        events.record("warning", "service_down", f"{name} is not responding: {result['error']}")
    elif status == "up" and was_down:
        events.record("info", "service_up", f"{name} is responding again", notify=True)
    known = set(previous["warnings"]) if previous else set()
    for warning in result["warnings"]:
        if warning not in known:
            events.record("warning", "service_warning", f"{name} reports: {warning}")
    STATE["services"][name] = {
        "name": name, "type": service["type"], "url": display_url(service["url"]),
        "status": status, "since": since, "checked_at": now, "failures": failures,
        "code": result["code"], "ms": result["ms"], "error": result["error"],
        "warnings": result["warnings"],
    }


def check_all():
    config = load_config()
    names = {service["name"] for service in config}
    for name in list(STATE["services"]):
        if name not in names:
            del STATE["services"][name]
    for service in config:
        update(service, check(service), int(time.time()))


def monitor(stop):
    while True:
        try:
            check_all()
        except Exception as exc:  # keep monitoring if one round goes wrong
            STATE["config_error"] = f"Service check failed: {exc}"[:300]
        if stop.wait(SERVICE_INTERVAL):
            return


def snapshot():
    order = [service["name"] for service in STATE["config"]]
    services = sorted(STATE["services"].values(), key=lambda s: order.index(s["name"]) if s["name"] in order else 0)
    return {
        "services": services,
        "config_error": STATE["config_error"],
        "config_file": config_source(),
        "interval_seconds": SERVICE_INTERVAL,
    }
