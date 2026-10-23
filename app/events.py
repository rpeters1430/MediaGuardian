import json
import os
import threading
import time
import urllib.request

from . import core

NTFY_URL = os.getenv("MG_NTFY_URL", "")
NTFY_TOKEN = os.getenv("MG_NTFY_TOKEN", "")
DISCORD_WEBHOOK = os.getenv("MG_DISCORD_WEBHOOK", "")
NOTIFY_LEVELS = {"warning", "critical"}
LAST_NOTIFY_ERROR = {"error": None}


def record(level, kind, message, notify=None):
    """Store an event and push warning/critical ones (or notify=True) to notification channels."""
    with core.DB_LOCK, core.connect() as db:
        db.execute(
            "INSERT INTO events(at,level,kind,message,acknowledged) VALUES (?,?,?,?,0)",
            (int(time.time()), level, kind, message[:1000]),
        )
    if notify is None:
        notify = level in NOTIFY_LEVELS
    if notify and (NTFY_URL or DISCORD_WEBHOOK):
        threading.Thread(target=send_notification, args=(level, message), daemon=True).start()


def list_events(limit=50, include_acknowledged=False):
    where = "" if include_acknowledged else "WHERE acknowledged=0"
    with core.DB_LOCK, core.connect() as db:
        rows = db.execute(
            f"SELECT id,at,level,kind,message,acknowledged FROM events {where} ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [dict(row) for row in rows]


def acknowledge_all():
    with core.DB_LOCK, core.connect() as db:
        db.execute("UPDATE events SET acknowledged=1 WHERE acknowledged=0")


def _post(url, body, headers):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Notification URL must start with http:// or https://")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read(1024)


def send_notification(level, message):
    title = f"MediaGuardian {level}"
    try:
        if NTFY_URL:
            headers = {"Title": title, "Priority": "high" if level == "critical" else "default"}
            if NTFY_TOKEN:
                headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
            _post(NTFY_URL, message.encode(), headers)
        if DISCORD_WEBHOOK:
            body = json.dumps({"content": f"**{title}**\n{message}"[:1900]}).encode()
            _post(DISCORD_WEBHOOK, body, {"Content-Type": "application/json"})
        LAST_NOTIFY_ERROR["error"] = None
    except (OSError, ValueError) as exc:
        LAST_NOTIFY_ERROR["error"] = str(exc)[:300]
