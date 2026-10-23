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
# Files modified more recently than this are inventoried but not probed, so
# downloads and Shrinkarr encodes aren't read while they are still being written.
SETTLE_SECONDS = max(0, int(os.getenv("MG_SETTLE_SECONDS", "300")))
# If at least MISSING_GUARD_MIN files and MISSING_GUARD_PERCENT of the library
# vanish in one scan, hold off marking them missing until the next scan agrees.
MISSING_GUARD_PERCENT = max(0.0, float(os.getenv("MG_MISSING_GUARD_PERCENT", "10")))
MISSING_GUARD_MIN = max(1, int(os.getenv("MG_MISSING_GUARD_MIN", "25")))
SHRINK_ALERT_PERCENT = max(0.0, float(os.getenv("MG_SHRINK_ALERT_PERCENT", "90")))
EXCLUDE_DIRS = {
    name.strip().lower() for name in os.getenv("MG_EXCLUDE", "").split(",") if name.strip()
} | {"lost+found", "$recycle.bin", "system volume information"}
SCAN_LOCK = threading.Lock()
DB_LOCK = threading.RLock()
STATE = {"running": False, "scanned": 0, "last_error": None, "last_finished": None, "started_at": None}


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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
        for column in ("ino", "dev", "nlink"):
            if column not in columns:
                db.execute(f"ALTER TABLE files ADD COLUMN {column} INTEGER")
        db.execute("CREATE INDEX IF NOT EXISTS ix_files_size ON files(size)")
        db.execute("""CREATE TABLE IF NOT EXISTS findings (
            kind TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
            detail TEXT, found_at INTEGER NOT NULL, PRIMARY KEY (kind, path)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS capacity (
            at INTEGER NOT NULL, label TEXT NOT NULL, total INTEGER NOT NULL, free INTEGER NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_capacity_at ON capacity(at)")
        db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("""CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL,
            finished_at INTEGER NOT NULL, trigger TEXT NOT NULL, complete INTEGER NOT NULL,
            seen INTEGER NOT NULL, probed INTEGER NOT NULL, new_files INTEGER NOT NULL,
            changed INTEGER NOT NULL, restored INTEGER NOT NULL, missing_marked INTEGER NOT NULL,
            missing_held INTEGER NOT NULL, new_issues INTEGER NOT NULL, errors INTEGER NOT NULL,
            error_sample TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, level TEXT NOT NULL,
            kind TEXT NOT NULL, message TEXT NOT NULL, acknowledged INTEGER NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS ix_events_ack ON events(acknowledged)")
    STATE["last_finished"] = last_scan_finished()


def reserve_scan():
    with SCAN_LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, scanned=0, last_error=None, started_at=int(time.time()))
        return True


def skip_dir(name):
    # Covers NAS/OS housekeeping folders such as @eaDir, #recycle, .Trash-1000 and .@__thumb.
    return name.startswith((".", "@", "#")) or name.lower() in EXCLUDE_DIRS


def skip_file(name):
    return (
        name.startswith("._")
        or ".shrinkarr.tmp" in name
        or Path(name).suffix.lower() not in EXTENSIONS
    )


def excluded(relpath):
    parts = Path(relpath).parts
    return skip_file(parts[-1]) or any(skip_dir(part) for part in parts[:-1])


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


def _note_error(summary, exc):
    summary["errors"] += 1
    if not summary["error_sample"]:
        summary["error_sample"] = str(exc)[:500]
    STATE["last_error"] = summary["error_sample"]


def _resolve_vanished(db, scan_id, present_before, unreadable, summary, dropped=()):
    """Decide what happens to indexed files the walk didn't see this time."""
    rows = db.execute(
        "SELECT path FROM files WHERE checked_at < ? AND status != 'missing'", (scan_id,)
    ).fetchall()
    vanished, keep, drop = [], [], []
    for row in rows:
        path = row["path"]
        if excluded(path) or any(path.startswith(prefix + "/") for prefix in dropped):
            drop.append((path,))
        elif any(path.startswith(prefix + "/") for prefix in unreadable):
            keep.append((path,))
        else:
            vanished.append(path)
    db.executemany("DELETE FROM files WHERE path=?", drop)
    db.executemany("UPDATE files SET checked_at=? WHERE path=?", [(scan_id, p) for (p,) in keep])
    if not vanished:
        return
    share = len(vanished) * 100 / max(present_before, 1)
    suspicious = summary["seen"] == 0 or (
        len(vanished) >= MISSING_GUARD_MIN and share >= MISSING_GUARD_PERCENT
    )
    previous = db.execute(
        "SELECT missing_held FROM scans WHERE complete=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if suspicious and not (previous and previous["missing_held"]):
        summary["missing_held"] = len(vanished)
        summary["missing_share"] = round(share, 1)
        return
    db.executemany(
        "UPDATE files SET status='missing',issue='Not observed in latest complete scan' WHERE path=?",
        [(p,) for p in vanished],
    )
    summary["missing_marked"] = len(vanished)
    summary["missing_sample"] = sorted(vanished)[:5]


def _save_scan(db, summary):
    db.execute(
        """INSERT INTO scans(started_at,finished_at,trigger,complete,seen,probed,new_files,changed,
        restored,missing_marked,missing_held,new_issues,errors,error_sample)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(summary[key] for key in (
            "started_at", "finished_at", "trigger", "complete", "seen", "probed", "new_files",
            "changed", "restored", "missing_marked", "missing_held", "new_issues", "errors",
            "error_sample",
        )),
    )


def scan_once(reserved=False, trigger="manual"):
    """Walk the media root once. Returns a summary dict, or False if a scan is already running."""
    if not reserved and not reserve_scan():
        return False
    summary = {
        "started_at": STATE["started_at"] or int(time.time()), "finished_at": 0,
        "trigger": trigger, "complete": 0, "seen": 0, "probed": 0, "new_files": 0,
        "changed": 0, "restored": 0, "missing_marked": 0, "missing_held": 0,
        "missing_share": 0, "missing_sample": [], "new_issues": 0, "issue_sample": [],
        "errors": 0, "error_sample": None, "fatal": None,
        "folder_findings": [], "shrunk": [], "new_findings": [],
    }
    from . import cleanup
    try:
        root = MEDIA.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Media root is not a directory")
        scan_id = time.time_ns()
        settled_before = time.time() - SETTLE_SECONDS
        unreadable = []
        download_dirs = cleanup.download_root_strings()
        leftover_before = time.time() - cleanup.LEFTOVER_HOURS * 3600
        pending = [root]
        with connect() as db:
            present_before = db.execute(
                "SELECT COUNT(*) FROM files WHERE status != 'missing'"
            ).fetchone()[0]
            os.scandir(root).close()  # an unreadable root must fail the scan, not empty it
            while pending and summary["seen"] < MAX_FILES:
                folder = pending.pop()
                tally = cleanup.FolderTally()
                try:
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            relpath = None
                            try:
                                if entry.is_symlink():
                                    tally.entries += 1
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    if skip_dir(entry.name):
                                        continue
                                    tally.entries += 1
                                    tally.subdirs += 1
                                    if entry.path not in download_dirs:
                                        pending.append(Path(entry.path))
                                    continue
                                if not entry.is_file(follow_symlinks=False) or cleanup.ignored_file(entry.name):
                                    continue
                                tally.entries += 1
                                junk = cleanup.junk_kind(entry.name)
                                suffix = Path(entry.name).suffix.lower()
                                if suffix in cleanup.SIDECAR_SUFFIXES and not junk:
                                    tally.sidecars += 1
                                    continue
                                if junk:
                                    st = entry.stat(follow_symlinks=False)
                                    if st.st_mtime < leftover_before:
                                        summary["folder_findings"].append((
                                            junk, str(Path(entry.path).relative_to(root)), st.st_size,
                                            "Older than a day; safe to delete once you've checked it",
                                        ))
                                    continue
                                if suffix in cleanup.AUDIO_SUFFIXES:
                                    tally.audio += 1
                                    continue
                                if skip_file(entry.name):
                                    continue
                                tally.videos += 1
                                relpath = str(Path(entry.path).relative_to(root))
                                st = entry.stat(follow_symlinks=False)
                                if cleanup.is_sample(entry.name, st.st_size):
                                    summary["folder_findings"].append((
                                        "sample_file", relpath, st.st_size, "Sample clip left by a release",
                                    ))
                                prior = db.execute(
                                    "SELECT size,mtime_ns,status,probe_json,issue FROM files WHERE path=?",
                                    (relpath,),
                                ).fetchone()
                                unchanged = prior and prior["size"] == st.st_size and prior["mtime_ns"] == st.st_mtime_ns
                                if prior is None:
                                    summary["new_files"] += 1
                                elif prior["status"] == "missing":
                                    summary["restored"] += 1
                                elif not unchanged:
                                    summary["changed"] += 1
                                    if prior["size"] and st.st_size < prior["size"] * (1 - SHRINK_ALERT_PERCENT / 100):
                                        summary["shrunk"].append((relpath, prior["size"], st.st_size))
                                status = "empty" if st.st_size == 0 else "inventory"
                                issue = "File is zero bytes" if st.st_size == 0 else None
                                probe = None
                                if unchanged and prior["status"] not in ("missing", "inventory"):
                                    status, probe, issue = prior["status"], prior["probe_json"], prior["issue"]
                                elif (
                                    st.st_size > 0
                                    and summary["probed"] < MAX_PROBES
                                    and st.st_mtime <= settled_before
                                ):
                                    status, probe, issue = probe_file(entry.path)
                                    summary["probed"] += 1
                                if status in ("empty", "probe_error") and not (unchanged and prior["status"] == status):
                                    summary["new_issues"] += 1
                                    if len(summary["issue_sample"]) < 5:
                                        summary["issue_sample"].append(relpath)
                                db.execute(
                                    """INSERT INTO files(path,size,mtime_ns,checked_at,status,probe_json,issue,ino,dev,nlink)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)
                                    ON CONFLICT(path) DO UPDATE SET
                                    size=excluded.size,mtime_ns=excluded.mtime_ns,
                                    checked_at=excluded.checked_at,status=excluded.status,
                                    probe_json=excluded.probe_json,issue=excluded.issue,
                                    ino=excluded.ino,dev=excluded.dev,nlink=excluded.nlink""",
                                    (relpath, st.st_size, st.st_mtime_ns, scan_id, status, probe, issue,
                                     st.st_ino, st.st_dev, st.st_nlink),
                                )
                                summary["seen"] += 1
                                STATE["scanned"] = summary["seen"]
                                if summary["seen"] % 100 == 0:
                                    db.commit()
                                if summary["seen"] >= MAX_FILES:
                                    break
                                if FILE_SLEEP:
                                    time.sleep(FILE_SLEEP)
                            except (OSError, ValueError) as exc:
                                _note_error(summary, exc)
                                if relpath:
                                    # Keep a file we failed to read from being reported missing.
                                    db.execute("UPDATE files SET checked_at=? WHERE path=?", (scan_id, relpath))
                    if folder != root:
                        finding = tally.finding(str(folder.relative_to(root)))
                        if finding:
                            summary["folder_findings"].append(finding)
                except OSError as exc:
                    _note_error(summary, exc)
                    unreadable.append(str(folder.relative_to(root)))
            if not pending and summary["seen"] < MAX_FILES:
                summary["complete"] = 1
                inside = [Path(d) for d in download_dirs if Path(d).is_relative_to(root)]
                _resolve_vanished(
                    db, scan_id, present_before, unreadable, summary,
                    [str(d.relative_to(root)) for d in inside],
                )
                summary["new_findings"] = cleanup.save_findings(db, summary["folder_findings"], cleanup.WALK_KINDS)
                if download_dirs:
                    downloads, _ = cleanup.walk_downloads()
                    summary["new_findings"] += cleanup.save_findings(
                        db, cleanup.download_findings(db, downloads), cleanup.DOWNLOAD_KINDS,
                    )
            summary["finished_at"] = int(time.time())
            _save_scan(db, summary)
    except Exception as exc:
        summary["fatal"] = str(exc)[:500]
        STATE["last_error"] = summary["fatal"]
    finally:
        with SCAN_LOCK:
            STATE["running"] = False
            STATE["last_finished"] = int(time.time())
    return summary


def last_scan_finished():
    with DB_LOCK, connect() as db:
        row = db.execute("SELECT MAX(finished_at) FROM scans").fetchone()
    return row[0]


def list_scans(limit=20):
    with DB_LOCK, connect() as db:
        rows = db.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 200),)
        ).fetchall()
    return [dict(row) for row in rows]


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


def library_stats():
    with DB_LOCK, connect() as db:
        rows = db.execute(
            """SELECT CASE WHEN instr(path,'/') > 0
               THEN substr(path,1,instr(path,'/')-1) ELSE 'Other' END AS library,
               COUNT(*) AS files, COALESCE(SUM(size),0) AS bytes
               FROM files WHERE status != 'missing'
               GROUP BY library ORDER BY files DESC"""
        ).fetchall()
    return [dict(row) for row in rows]
