"""Settings edited on the dashboard. A saved value wins over the environment, which wins over the default."""
import json
import os
import re
import threading
from pathlib import Path

from . import cleanup, core, events, guardian, playback, stack

LOCK = threading.Lock()
BUILTIN_EXCLUDES = {"lost+found", "$recycle.bin", "system volume information"}


def _csv(value):
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _window(value):
    guardian.parse_window(value)  # raises ValueError with a readable message
    return value


def _url(value):
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("must start with http:// or https://")
    return value


def _paths(value):
    for path in _csv(value):
        if not path.startswith("/"):
            raise ValueError(f"{path} must be an absolute path inside the container, like /downloads")
    return value


# key: (kind, default, minimum, maximum, group, label, help)
SPEC = {
    "MG_SCAN_INTERVAL_MINUTES": ("int", 360, 0, 10080, "Scanning", "Minutes between automatic scans",
                                 "0 turns automatic scans off. Every scan walks the whole share, so frequent scans keep hard drives from spinning down."),
    "MG_SCAN_WINDOW": ("window", "", None, None, "Scanning", "Quiet hours for automatic scans",
                       "Only start automatic scans in these hours, e.g. 1-7 for 01:00 to 06:59. Leave empty for any time."),
    "MG_STARTUP_SCAN_DELAY": ("int", 120, 0, 3600, "Scanning", "Seconds before the first scan after a restart", ""),
    "MG_MAX_PROBES_PER_SCAN": ("int", 200, 0, 5000, "Scanning", "Files read with ffprobe per scan",
                               "New, changed and not-yet-checked files. Probing reads only file headers."),
    "MG_PROBE_TIMEOUT": ("int", 12, 2, 120, "Scanning", "Seconds before ffprobe gives up on a file", ""),
    "MG_SETTLE_SECONDS": ("int", 300, 0, 86400, "Scanning", "Wait this many seconds after a file changes before probing it",
                          "Keeps downloads and Shrinkarr encodes from being read while they are still being written."),
    "MG_FILE_SLEEP": ("float", 0.02, 0, 1, "Scanning", "Pause between files (seconds)", "Higher values make scans gentler on the disks and slower."),
    "MG_MAX_FILES": ("int", 250000, 1, 5000000, "Scanning", "Most files to index", ""),
    "MG_EXCLUDE": ("csv", "", None, None, "Scanning", "Folder names to skip",
                   "Comma-separated, e.g. Extras,Backups. Folders starting with . @ or # are always skipped."),
    "MG_MISSING_GUARD_MIN": ("int", 25, 1, 1000000, "Alerts", "Files that must vanish at once to hold a scan",
                             "With the percentage below: when this many files disappear in one scan, MediaGuardian alerts first and only marks them missing if the next scan agrees."),
    "MG_MISSING_GUARD_PERCENT": ("float", 10, 0, 100, "Alerts", "Share of the library that must vanish at once (%)", ""),
    "MG_SHRINK_ALERT_PERCENT": ("float", 90, 0, 100, "Alerts", "Warn when a file shrinks by more than (%)", ""),
    "MG_DISK_WARN_PERCENT": ("float", 10, 0, 100, "Alerts", "Warn below this much free space (%)", ""),
    "MG_DISK_WARN_DAYS": ("int", 30, 0, 3650, "Alerts", "Warn when a disk will be full within (days)", ""),
    "MG_NTFY_URL": ("url", "", None, None, "Notifications", "ntfy topic URL", "e.g. https://ntfy.sh/your-topic. Warnings and critical alerts are pushed here."),
    "MG_NTFY_TOKEN": ("secret", "", None, None, "Notifications", "ntfy access token", "Only needed for a protected topic."),
    "MG_DISCORD_WEBHOOK": ("secret_url", "", None, None, "Notifications", "Discord webhook URL", ""),
    "MG_DOWNLOADS": ("paths", "", None, None, "Downloads", "Download folders inside the container",
                     "Comma-separated, e.g. /downloads. The folder must also be mounted read-only in compose.yaml."),
    "MG_UNIMPORTED_DAYS": ("float", 7, 0, 3650, "Downloads", "Report downloads missing from the library after (days)", ""),
    "MG_SERVICE_INTERVAL": ("int", 60, 15, 3600, "Apps", "Seconds between up/down checks", ""),
    "MG_CROSSCHECK_MINUTES": ("int", 60, 0, 1440, "Apps", "Minutes between library checks",
                              "Compares Sonarr, Radarr and your media server with the disk. API calls only; 0 means only after scans."),
    "MG_LANGUAGES": ("csv", "eng", None, None, "Playback", "Your languages", "Audio or subtitles in these are fine, e.g. eng,spa."),
    "MG_DIRECT_PLAY_VIDEO": ("csv", "h264,hevc,av1,vp9", None, None, "Playback", "Video codecs your players handle", ""),
    "MG_DIRECT_PLAY_AUDIO": ("csv", "aac,ac3,eac3,mp3,opus,flac,vorbis", None, None, "Playback", "Audio codecs your players handle", ""),
}
SECRET_KINDS = {"secret", "secret_url"}


def settings_file():
    return core.BASE / "settings.json"


def load_saved():
    try:
        data = json.loads(settings_file().read_text())
        return {k: v for k, v in data.items() if k in SPEC} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_private(path: Path, text):
    """Write a file only the container user can read, replacing it atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def coerce(key, value):
    kind, _, low, high = SPEC[key][:4]
    if kind in ("int", "float"):
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if not low <= number <= high:
            raise ValueError(f"must be between {low} and {high}")
        return number
    value = str(value if value is not None else "").strip()
    if kind == "window":
        return _window(value)
    if kind in ("url", "secret_url"):
        return _url(value)
    if kind == "paths":
        return _paths(value)
    if kind == "csv":
        return ",".join(_csv(value))
    return value


def resolve(saved=None):
    """{key: (value, source)} where source is saved, environment or default."""
    saved = load_saved() if saved is None else saved
    result = {}
    for key, spec in SPEC.items():
        if key in saved:
            result[key] = (saved[key], "saved")
            continue
        env = os.getenv(key)
        if env is not None and env.strip() != "":
            try:
                result[key] = (coerce(key, env), "environment")
                continue
            except ValueError:
                pass  # a bad environment value falls back to the default
        result[key] = (spec[1], "default")
    return result


def apply(values=None):
    """Push the current values into the modules that use them."""
    v = {k: value for k, (value, _) in (values or resolve()).items()}
    guardian.SCAN_INTERVAL_MINUTES = v["MG_SCAN_INTERVAL_MINUTES"]
    guardian.SCAN_WINDOW = v["MG_SCAN_WINDOW"]
    guardian.WINDOW = guardian.parse_window(v["MG_SCAN_WINDOW"])
    guardian.STARTUP_DELAY = v["MG_STARTUP_SCAN_DELAY"]
    guardian.CROSSCHECK_MINUTES = v["MG_CROSSCHECK_MINUTES"]
    core.MAX_PROBES = v["MG_MAX_PROBES_PER_SCAN"]
    core.PROBE_TIMEOUT = v["MG_PROBE_TIMEOUT"]
    core.SETTLE_SECONDS = v["MG_SETTLE_SECONDS"]
    core.FILE_SLEEP = v["MG_FILE_SLEEP"]
    core.MAX_FILES = v["MG_MAX_FILES"]
    core.EXCLUDE_DIRS = {n.lower() for n in _csv(v["MG_EXCLUDE"])} | BUILTIN_EXCLUDES
    core.MISSING_GUARD_MIN = v["MG_MISSING_GUARD_MIN"]
    core.MISSING_GUARD_PERCENT = v["MG_MISSING_GUARD_PERCENT"]
    core.SHRINK_ALERT_PERCENT = v["MG_SHRINK_ALERT_PERCENT"]
    cleanup.DISK_WARN_PERCENT = v["MG_DISK_WARN_PERCENT"]
    cleanup.DISK_WARN_DAYS = v["MG_DISK_WARN_DAYS"]
    cleanup.DOWNLOAD_ROOTS = [Path(p) for p in _csv(v["MG_DOWNLOADS"])]
    cleanup.UNIMPORTED_DAYS = v["MG_UNIMPORTED_DAYS"]
    events.NTFY_URL = v["MG_NTFY_URL"]
    events.NTFY_TOKEN = v["MG_NTFY_TOKEN"]
    events.DISCORD_WEBHOOK = v["MG_DISCORD_WEBHOOK"]
    stack.SERVICE_INTERVAL = v["MG_SERVICE_INTERVAL"]
    playback.LANGUAGES = {playback.normalize_language(c) for c in _csv(v["MG_LANGUAGES"])} or {"eng"}
    playback.DIRECT_VIDEO = {c.lower() for c in _csv(v["MG_DIRECT_PLAY_VIDEO"])}
    playback.DIRECT_AUDIO = {c.lower() for c in _csv(v["MG_DIRECT_PLAY_AUDIO"])}


def describe():
    """Settings for the dashboard. Secrets are reported as set or not, never returned."""
    current = resolve()
    out = []
    for key, (kind, default, low, high, group, label, help_text) in SPEC.items():
        value, source = current[key]
        secret = kind in SECRET_KINDS
        out.append({
            "key": key, "kind": kind, "group": group, "label": label, "help": help_text,
            "min": low, "max": high, "default": "" if secret else default, "source": source,
            "value": "" if secret else value, "is_set": bool(value),
        })
    return out


def save(changes):
    """changes: {key: value | None}. None resets the key to the environment or default.
    For secrets, an empty string means keep the saved value. Returns {key: error} on failure."""
    with LOCK:
        saved = load_saved()
        errors = {}
        for key, value in changes.items():
            if key not in SPEC:
                errors[key] = "Unknown setting"
                continue
            if value is None:
                saved.pop(key, None)
                continue
            if SPEC[key][0] in SECRET_KINDS and value == "":
                continue
            try:
                saved[key] = coerce(key, value)
            except ValueError as exc:
                errors[key] = str(exc)
        if errors:
            return errors
        write_private(settings_file(), json.dumps(saved, indent=2))
        apply(resolve(saved))
    return {}


# --- app list (services.json) -----------------------------------------------

def services_editable():
    return not os.getenv("MG_SERVICES", "").strip()


def list_services():
    return [
        {
            "name": s["name"], "type": s["type"], "url": s["url"], "has_key": bool(s["api_key"]),
            "path_map": s["path_map"],
        }
        for s in stack.load_config()
    ]


def save_services(items):
    """Validate and write services.json. A missing api_key keeps the key of the app named in keep_key_from."""
    if not services_editable():
        raise ValueError("Apps are set by MG_SERVICES in compose.yaml. Remove it there to edit apps here.")
    old = {s["name"]: s for s in stack.load_config()}
    raw, names = [], set()
    for item in items:
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("Every app needs a name")
        if name in names:
            raise ValueError(f"Two apps are called {name}")
        names.add(name)
        api_key = item.get("api_key")
        if api_key in (None, ""):
            api_key = old.get(item.get("keep_key_from") or name, {}).get("api_key", "")
        entry = {"name": name, "type": item.get("type", "generic"), "url": str(item.get("url", "")).strip()}
        if api_key:
            entry["api_key"] = api_key
        if item.get("path_map"):
            entry["path_map"] = item["path_map"]
        raw.append(entry)
    text = json.dumps(raw, indent=2)
    stack.parse_config(text)  # raises ValueError for a bad entry before anything is written
    write_private(stack.services_file(), text)
    stack.load_config()
    return list_services()
