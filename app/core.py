import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".mpg", ".mpeg"}
BASE = Path(os.getenv("MG_DATA_DIR", "/data"))
MEDIA = Path(os.getenv("MG_MEDIA_ROOT", "/media"))
MAX_PROBES = max(0, int(os.getenv("MG_MAX_PROBES_PER_SCAN", "30")))
PROBE_TIMEOUT = max(2, int(os.getenv("MG_PROBE_TIMEOUT", "12")))
FILE_SLEEP = max(0, float(os.getenv("MG_FILE_SLEEP", "0.02")))
MAX_FILES = max(1, int(os.getenv("MG_MAX_FILES", "250000")))
SCAN_LOCK = threading.Lock()
DB_LOCK = threading.RLock()
STATE = {"running": False, "scanned": 0, "last_error": None, "last_finished": None}


def connect():
    BASE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(BASE / "mediaguardian.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    with DB_LOCK, connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, checked_at INTEGER NOT NULL,
            status TEXT NOT NULL, probe_json TEXT, issue TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_files_status ON files(status)")


def reserve_scan():
    with SCAN_LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, scanned=0, last_error=None)
        return True


def probe_file(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,format_name:stream=codec_name,codec_type,width,height",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
        )
        if result.returncode:
            return "probe_error", None, result.stderr[:500]
        return "ok", json.dumps(json.loads(result.stdout)), None
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return "probe_error", None, str(exc)[:500]


def scan_once(reserved=False):
    if not reserved and not reserve_scan():
        return False
    try:
        root = MEDIA.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Media root is not a directory")
        scan_id = time.time_ns()
        count = 0
        probed = 0
        pending = [root]
        with DB_LOCK, connect() as db:
            while pending and count < MAX_FILES:
                folder = pending.pop()
                try:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            try:
                                if entry.is_symlink():
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    pending.append(Path(entry.path))
                                    continue
                                if not entry.is_file(follow_symlinks=False):
                                    continue
                                if Path(entry.name).suffix.lower() not in EXTENSIONS:
                                    continue
                                st = entry.stat(follow_symlinks=False)
                                relpath = str(Path(entry.path).relative_to(root))
                                prior = db.execute(
                                    "SELECT size,mtime_ns,status,probe_json,issue FROM files WHERE path=?",
                                    (relpath,),
                                ).fetchone()
                                status = "empty" if st.st_size == 0 else "inventory"
                                issue = "File is zero bytes" if st.st_size == 0 else None
                                probe = None
                                if prior and prior["size"] == st.st_size and prior["mtime_ns"] == st.st_mtime_ns:
                                    status, probe, issue = prior["status"], prior["probe_json"], prior["issue"]
                                elif st.st_size > 0 and probed < MAX_PROBES:
                                    status, probe, issue = probe_file(entry.path)
                                    probed += 1
                                db.execute(
                                    """INSERT INTO files(path,size,mtime_ns,checked_at,status,probe_json,issue)
                                    VALUES (?,?,?,?,?,?,?)
                                    ON CONFLICT(path) DO UPDATE SET
                                    size=excluded.size,mtime_ns=excluded.mtime_ns,
                                    checked_at=excluded.checked_at,status=excluded.status,
                                    probe_json=excluded.probe_json,issue=excluded.issue""",
                                    (relpath, st.st_size, st.st_mtime_ns, scan_id, status, probe, issue),
                                )
                                count += 1
                                STATE["scanned"] = count
                                if count >= MAX_FILES:
                                    break
                                if FILE_SLEEP:
                                    time.sleep(FILE_SLEEP)
                            except (OSError, ValueError) as exc:
                                STATE["last_error"] = str(exc)[:500]
                except OSError as exc:
                    STATE["last_error"] = str(exc)[:500]
            if not pending and count < MAX_FILES and not STATE["last_error"]:
                db.execute(
                    "UPDATE files SET status='missing',issue='Not observed in latest complete scan' "
                    "WHERE checked_at < ? AND status != 'missing'",
                    (scan_id,),
                )
    except Exception as exc:
        STATE["last_error"] = str(exc)[:500]
    finally:
        with SCAN_LOCK:
            STATE["running"] = False
            STATE["last_finished"] = int(time.time())
    return True


def stats():
    with DB_LOCK, connect() as db:
        rows = db.execute(
            "SELECT status,COUNT(*) AS n,COALESCE(SUM(size),0) AS bytes FROM files GROUP BY status"
        ).fetchall()
    return {
        "statuses": {row["status"]: row["n"] for row in rows},
        "total_files": sum(row["n"] for row in rows),
        "bytes": sum(row["bytes"] for row in rows),
        "scan": dict(STATE),
    }


def list_issues(limit=100):
    with DB_LOCK, connect() as db:
        rows = db.execute(
            "SELECT path,size,status,issue FROM files "
            "WHERE status IN ('empty','probe_error','missing') ORDER BY status,path LIMIT ?",
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [dict(row) for row in rows]
