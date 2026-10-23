"""Space reports: copied or unimported downloads, leftovers, empty folders and disk capacity."""
import os
import re
import shutil
import time
from pathlib import Path

from . import core

DOWNLOAD_ROOTS = [Path(p.strip()) for p in os.getenv("MG_DOWNLOADS", "").split(",") if p.strip()]
UNIMPORTED_DAYS = max(0, float(os.getenv("MG_UNIMPORTED_DAYS", "7")))
LEFTOVER_HOURS = 24
MIN_MATCH_BYTES = 50 * 1024 * 1024
SAMPLE_MAX_BYTES = 500 * 1024 * 1024
COMPARE_LIMIT = 500
COMPARE_CHUNK = 64 * 1024
CAPACITY_SAMPLE_SECONDS = 3600
CAPACITY_KEEP_DAYS = 90
DISK_WARN_DAYS = max(0, int(os.getenv("MG_DISK_WARN_DAYS", "30")))
DISK_WARN_PERCENT = max(0.0, float(os.getenv("MG_DISK_WARN_PERCENT", "10")))

INCOMPLETE_SUFFIXES = {".part", ".!qb", ".!ut", ".crdownload", ".partial"}
SIDECAR_SUFFIXES = {".nfo", ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".ape", ".wma"}
IGNORED_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
SAMPLE_RE = re.compile(r"(^|[^a-z])sample([^a-z]|$)", re.IGNORECASE)

KINDS = {
    "copied_download": "Download stored twice (not hardlinked)",
    "download_not_in_library": "Download with no library copy",
    "shrinkarr_leftover": "Shrinkarr leftover file",
    "incomplete_download": "Incomplete download in the library",
    "sample_file": "Sample clip",
    "empty_folder": "Empty folder",
    "no_video_folder": "Folder with subtitles or metadata but no video",
}

DOWNLOAD_KINDS = ("copied_download", "download_not_in_library")
# "unreadable" is found by the walk but shown with the stack checks.
WALK_KINDS = tuple(kind for kind in KINDS if kind not in DOWNLOAD_KINDS) + ("unreadable",)


def download_root_strings():
    return {str(root.resolve()) for root in DOWNLOAD_ROOTS if root.exists()}


def ignored_file(name):
    return name.startswith("._") or name.lower() in IGNORED_FILES


def junk_kind(name):
    if ".shrinkarr.bak" in name or ".shrinkarr.tmp" in name:
        return "shrinkarr_leftover"
    if Path(name).suffix.lower() in INCOMPLETE_SUFFIXES:
        return "incomplete_download"
    return None


def is_sample(name, size):
    return size < SAMPLE_MAX_BYTES and bool(SAMPLE_RE.search(Path(name).stem))


class FolderTally:
    """What one folder directly contains, to spot empty and video-less folders."""

    def __init__(self):
        self.entries = 0
        self.subdirs = 0
        self.videos = 0
        self.sidecars = 0
        self.audio = 0

    def finding(self, relfolder):
        if self.entries == 0:
            return ("empty_folder", relfolder, 0, "Contains nothing (NAS thumbnail folders aside)")
        if not self.videos and not self.subdirs and not self.audio and self.sidecars:
            return (
                "no_video_folder", relfolder, 0,
                f"{self.sidecars} subtitle/metadata files but no video; the video may have been deleted or moved",
            )
        return None


def walk_downloads():
    """List video files under the download roots. Paths are absolute container paths."""
    files, errors = [], 0
    for root in DOWNLOAD_ROOTS:
        pending = [root]
        while pending:
            folder = pending.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if not core.skip_dir(entry.name):
                                    pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False) and not core.skip_file(entry.name):
                                st = entry.stat(follow_symlinks=False)
                                files.append({
                                    "path": entry.path, "name": entry.name, "size": st.st_size,
                                    "mtime": st.st_mtime, "ino": st.st_ino, "dev": st.st_dev,
                                    "nlink": st.st_nlink,
                                })
                        except OSError:
                            errors += 1
            except OSError:
                errors += 1
    return files, errors


def same_content(a, b, size):
    """Cheap identity check: compare the first and last 64 KiB of two equal-sized files."""
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            if fa.read(COMPARE_CHUNK) != fb.read(COMPARE_CHUNK):
                return False
            if size > COMPARE_CHUNK:
                fa.seek(size - COMPARE_CHUNK)
                fb.seek(size - COMPARE_CHUNK)
                return fa.read(COMPARE_CHUNK) == fb.read(COMPARE_CHUNK)
            return True
    except OSError:
        return False


def download_findings(db, downloads, now=None):
    now = now or time.time()
    by_size = {}
    for row in db.execute(
        "SELECT path,size,ino,dev FROM files WHERE status!='missing' AND ino IS NOT NULL AND size>=?",
        (MIN_MATCH_BYTES,),
    ):
        by_size.setdefault(row["size"], []).append(row)
    media_root = core.MEDIA.resolve()
    findings, compared = [], 0
    for item in downloads:
        if item["size"] < MIN_MATCH_BYTES or is_sample(item["name"], item["size"]):
            continue
        matches = by_size.get(item["size"], [])
        if any(m["ino"] == item["ino"] and m["dev"] == item["dev"] for m in matches):
            continue  # hardlinked into the library: no extra space used
        copy, unchecked = None, False
        for match in matches:
            if compared >= COMPARE_LIMIT:
                unchecked = True
                break
            compared += 1
            if same_content(item["path"], media_root / match["path"], item["size"]):
                copy = match
                break
        if copy:
            detail = f"Same file as {copy['path']}, stored twice"
            if copy["dev"] != item["dev"]:
                detail += ". It is on a different volume from the library, so it can't be hardlinked"
            findings.append(("copied_download", item["path"], item["size"], detail))
        elif not unchecked and item["nlink"] == 1 and now - item["mtime"] > UNIMPORTED_DAYS * 86400:
            findings.append((
                "download_not_in_library", item["path"], item["size"],
                "No matching library file. It was never imported, or the library copy was replaced "
                "(for example re-encoded by Shrinkarr), so this download now uses its own space",
            ))
    return findings


def volumes_match():
    """None when no download folder is set, else whether hardlinks between it and the library can work."""
    if not DOWNLOAD_ROOTS:
        return None
    try:
        media_dev = core.MEDIA.stat().st_dev
        return all(root.stat().st_dev == media_dev for root in DOWNLOAD_ROOTS)
    except OSError:
        return None


def save_findings(db, findings, kinds, source=None):
    """Replace stored findings of the given kinds (from one source). Returns the ones not seen before."""
    where = f"kind IN ({','.join('?' * len(kinds))}) AND source IS ?"
    params = (*kinds, source)
    known = {
        (row["kind"], row["path"]): row["found_at"]
        for row in db.execute(f"SELECT kind,path,found_at FROM findings WHERE {where}", params)
    }
    now = int(time.time())
    db.execute(f"DELETE FROM findings WHERE {where}", params)
    db.executemany(
        "INSERT OR REPLACE INTO findings(kind,path,size,detail,found_at,source) VALUES (?,?,?,?,?,?)",
        [(k, p, s, d, known.get((k, p), now), source) for k, p, s, d in findings],
    )
    return [f for f in findings if (f[0], f[1]) not in known]


def summary():
    with core.DB_LOCK, core.connect() as db:
        rows = db.execute(
            "SELECT kind,COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes FROM findings GROUP BY kind"
        ).fetchall()
    counts = {row["kind"]: dict(row) for row in rows}
    return [
        {"kind": kind, "label": label, **counts.get(kind, {"count": 0, "bytes": 0})}
        for kind, label in KINDS.items()
    ]


def list_findings(kind=None, limit=200):
    query = "SELECT kind,path,size,detail,found_at FROM findings"
    params = []
    if kind:
        query += " WHERE kind=?"
        params.append(kind)
    query += " ORDER BY size DESC, path LIMIT ?"
    params.append(min(max(limit, 1), 1000))
    with core.DB_LOCK, core.connect() as db:
        return [dict(row) for row in db.execute(query, params)]


def volumes():
    targets = {"Media": core.MEDIA}
    try:
        media_dev = core.MEDIA.stat().st_dev
    except OSError:
        return {}
    for root in DOWNLOAD_ROOTS:
        try:
            if root.stat().st_dev != media_dev:
                targets[f"Downloads ({root})"] = root
        except OSError:
            continue
    return targets


def sample_capacity(now=None, force=False):
    now = int(now or time.time())
    with core.DB_LOCK, core.connect() as db:
        last = db.execute("SELECT MAX(at) FROM capacity").fetchone()[0]
        if not force and last and now - last < CAPACITY_SAMPLE_SECONDS:
            return False
        for label, path in volumes().items():
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            db.execute(
                "INSERT INTO capacity(at,label,total,free) VALUES (?,?,?,?)",
                (now, label, usage.total, usage.free),
            )
        db.execute("DELETE FROM capacity WHERE at < ?", (now - CAPACITY_KEEP_DAYS * 86400,))
    return True


def forecast(samples):
    """samples: [(at, total, free)] oldest first. Projects days until full from the last 30 days."""
    at, total, free = samples[-1]
    result = {
        "total": total, "free": free, "used_percent": round((total - free) * 100 / total, 1) if total else 0,
        "growth_per_day": None, "days_left": None,
    }
    # A resized volume changes the total; only compare samples taken at the current size.
    recent = [s for s in samples if s[0] >= at - 30 * 86400 and s[1] == total]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < 86400:
        return result  # need at least a day of history
    xs = [s[0] / 86400 for s in recent]
    ys = [s[1] - s[2] for s in recent]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var if var else 0
    result["growth_per_day"] = round(slope)
    if slope > 0:
        result["days_left"] = round(free / slope, 1)
    return result


def capacity():
    with core.DB_LOCK, core.connect() as db:
        rows = db.execute("SELECT at,label,total,free FROM capacity ORDER BY at").fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["label"], []).append((row["at"], row["total"], row["free"]))
    return [{"label": label, **forecast(samples)} for label, samples in grouped.items()]


def capacity_problem(item):
    free_percent = 100 - item["used_percent"]
    if free_percent < DISK_WARN_PERCENT:
        return f"{item['label']} has {free_percent:.1f}% free space left"
    if item["days_left"] is not None and item["days_left"] < DISK_WARN_DAYS:
        return f"{item['label']} will be full in about {item['days_left']:.0f} days at the current rate"
    return None


def get_flag(db, key):
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_flag(db, key, value):
    db.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
