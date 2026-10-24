"""Compare what Sonarr, Radarr, Jellyfin, Emby and Plex think exists with what is on disk."""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import PurePosixPath

from . import cleanup, core, stack

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT = 30
NEW_FILE_GRACE_SECONDS = 86400  # give media servers a day to pick up new files
STUCK_STATES = {"importPending", "importBlocked", "importFailed", "failedPending"}
EXTRAS_DIRS = {
    "extras", "featurettes", "behind the scenes", "deleted scenes", "interviews",
    "scenes", "shorts", "trailers", "other", "clips", "samples",
}
EXTRAS_SUFFIXES = (
    "-trailer", "-sample", "-featurette", "-behindthescenes", "-deleted", "-deletedscene",
    "-interview", "-scene", "-short", "-other", "-clip", "-extra",
)
SERVER_TYPES = {"jellyfin", "emby", "plex"}
EPISODE_LOOKUPS = 50  # most series checked file by file per run
ARR_TYPES = {"sonarr", "radarr"}

KINDS = {
    "arr_missing_file": "Sonarr/Radarr expect a file that isn't on disk",
    "arr_stuck_import": "Download stuck in the Sonarr/Radarr queue",
    "arr_not_tracked": "Folder Sonarr/Radarr don't manage",
    "server_missing_file": "Media server item whose file is gone",
    "server_not_in_library": "File your media server hasn't picked up",
    "unreadable": "File or folder MediaGuardian can't read",
}
STATE = {"services": {}, "last_run": None}
RUN_LOCK = threading.Lock()


def is_extra(relpath):
    path = PurePosixPath(relpath)
    if any(part.lower() in EXTRAS_DIRS for part in path.parts[:-1]):
        return True
    return path.stem.lower().endswith(EXTRAS_SUFFIXES) or cleanup.is_sample(path.name, 0)


def fetch_json(url, headers):
    request = urllib.request.Request(
        url, headers={"User-Agent": "MediaGuardian", "Accept": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    return json.loads(body)


# --- path mapping -------------------------------------------------------------

def _resolve_ci(parts):
    """Find parts under the media root, matching each folder name case-insensitively."""
    current = core.MEDIA
    rel = []
    for part in parts:
        candidate = current / part
        if not candidate.is_dir():
            try:
                names = [n for n in os.listdir(current) if n.lower() == part.lower()]
            except OSError:
                return None
            if not names or not (current / names[0]).is_dir():
                return None
            candidate = current / names[0]
        rel.append(candidate.name)
        current = candidate
    return "/".join(rel)


def map_root(remote, overrides):
    """Media-root-relative folder for an app's library folder, or None if it can't be found."""
    remote = remote.replace("\\", "/").rstrip("/") or "/"
    for prefix in sorted(overrides, key=len, reverse=True):
        if remote == prefix or remote.startswith(prefix.rstrip("/") + "/"):
            rest = remote[len(prefix):].strip("/")
            return "/".join(p for p in (overrides[prefix], rest) if p)
    parts = [p for p in remote.split("/") if p]
    for start in range(len(parts)):  # longest matching tail wins
        rel = _resolve_ci(parts[start:])
        if rel is not None:
            return rel
    return None


def to_local(path, roots):
    """Translate an app's file path to a media-root-relative path using mapped roots."""
    path = path.replace("\\", "/")
    for remote in sorted(roots, key=len, reverse=True):
        if path == remote or path.startswith(remote.rstrip("/") + "/"):
            rest = path[len(remote):].strip("/")
            return "/".join(p for p in (roots[remote], rest) if p)
    return None


def map_roots(remotes, overrides):
    roots, unmapped = {}, []
    for remote in remotes:
        local = map_root(remote, overrides)
        if local is None:
            unmapped.append(remote)
        else:
            roots[remote.replace("\\", "/").rstrip("/") or "/"] = local
    return roots, unmapped


def under(path, folder):
    return folder == "" or path == folder or path.startswith(folder + "/")


def ancestors(path):
    """'a/b/c.mkv' -> ['', 'a', 'a/b']"""
    parts = path.split("/")[:-1]
    return [""] + ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


# --- per-app checks ----------------------------------------------------------

def arr_queue(service, base, headers):
    data = fetch_json(f"{base}/api/v3/queue?page=1&pageSize=500&includeUnknownSeriesItems=true&includeUnknownMovieItems=true", headers)
    findings = []
    for item in data.get("records", []):
        state = item.get("trackedDownloadState")
        status = item.get("trackedDownloadStatus")
        if state not in STUCK_STATES and status not in ("warning", "error"):
            continue
        messages = [m for group in item.get("statusMessages") or [] for m in group.get("messages") or []]
        reason = "; ".join(messages) or item.get("errorMessage") or state or status
        findings.append((
            "arr_stuck_import", f"{service['name']}: {item.get('title', 'Unknown download')}", item.get("size") or 0,
            f"{state or status}: {reason}"[:500],
        ))
    return findings


def check_arr(service, index):
    base, headers = service["url"], {"X-Api-Key": service["api_key"]}
    remotes = [r["path"] for r in fetch_json(f"{base}/api/v3/rootfolder", headers)]
    roots, unmapped = map_roots(remotes, service["path_map"])
    findings = arr_queue(service, base, headers)
    radarr = service["type"] == "radarr"
    items = fetch_json(f"{base}/api/v3/{'movie' if radarr else 'series'}", headers)
    videos = [p for p in index if not is_extra(p)]
    per_folder = {}
    for path in videos:
        for folder in ancestors(path):
            per_folder[folder] = per_folder.get(folder, 0) + 1
    tracked, suspects = set(), []
    for item in items:
        folder = to_local(item.get("path") or "", roots)
        if folder is None:
            continue
        tracked.add(folder)
        label = f"{item.get('title')} ({item.get('year')})" if item.get("year") else str(item.get("title"))
        if radarr:
            file_path = (item.get("movieFile") or {}).get("path")
            local = to_local(file_path, roots) if file_path else None
            if item.get("hasFile") and local and local not in index:
                findings.append(("arr_missing_file", local, 0, f"{service['name']} expects this file for {label}"))
        else:
            expected = (item.get("statistics") or {}).get("episodeFileCount") or 0
            present = per_folder.get(folder, 0)
            if expected > present:
                suspects.append((item, folder, label, expected, present))
    # Counts can disagree for harmless reasons, so ask Sonarr for the exact files of each
    # series that doesn't add up and report only the files that really are gone.
    for item, folder, label, expected, present in suspects[:EPISODE_LOOKUPS]:
        try:
            files = fetch_json(f"{base}/api/v3/episodefile?seriesId={item['id']}", headers)
        except Exception:  # fall back to the count when the lookup fails
            files = None
        if files is None:
            findings.append((
                "arr_missing_file", folder, 0,
                f"{service['name']} lists {expected} episode files for {label}, but {present} are on disk",
            ))
            continue
        for episode_file in files:
            local = to_local(episode_file.get("path") or "", roots)
            if local and local not in index:
                findings.append(("arr_missing_file", local, 0, f"{service['name']} expects this file for {label}"))
    untracked = {}
    for root in roots.values():
        for path in videos:
            if not under(path, root) or any(a in tracked for a in ancestors(path)):
                continue
            rest = path[len(root):].strip("/") if root else path
            top = "/".join(p for p in (root, rest.split("/")[0]) if p)
            untracked[top] = untracked.get(top, 0) + 1
    for folder, count in untracked.items():
        findings.append((
            "arr_not_tracked", folder, 0,
            f"{count} video files in a {service['name']} root folder that {service['name']} doesn't manage",
        ))
    return findings, unmapped


def server_headers(service):
    token = service["api_key"]
    if service["type"] == "plex":
        return {"X-Plex-Token": token}
    if service["type"] == "jellyfin":
        # Jellyfin 10.11 turns off the X-Emby-Token header by default.
        return {"Authorization": f'MediaBrowser Token="{token}"'}
    return {"X-Emby-Token": token}


def server_paths(service, headers):
    """(library folders, item file paths) as the media server sees them."""
    base = service["url"]
    if service["type"] == "plex":
        sections = fetch_json(f"{base}/library/sections", headers)["MediaContainer"].get("Directory", [])
        remotes, files = [], []
        for section in sections:
            if section.get("type") not in ("movie", "show"):
                continue
            remotes += [loc["path"] for loc in section.get("Location", [])]
            item_type = 1 if section["type"] == "movie" else 4
            data = fetch_json(f"{base}/library/sections/{section['key']}/all?type={item_type}", headers)
            for item in data["MediaContainer"].get("Metadata", []):
                files += [part["file"] for media in item.get("Media", []) for part in media.get("Part", []) if part.get("file")]
        return remotes, files
    prefix = "/emby" if service["type"] == "emby" else ""
    folders = fetch_json(f"{base}{prefix}/Library/VirtualFolders", headers)
    remotes = [
        loc for folder in folders
        if (folder.get("CollectionType") or "mixed") in ("movies", "tvshows", "mixed", "homevideos")
        for loc in folder.get("Locations", [])
    ]
    query = urllib.parse.urlencode({
        "Recursive": "true", "IncludeItemTypes": "Movie,Episode,Video", "Fields": "Path",
        "EnableImages": "false", "EnableUserData": "false",
    })
    items = fetch_json(f"{base}{prefix}/Items?{query}", headers).get("Items", [])
    return remotes, [item["Path"] for item in items if item.get("Path")]


def check_server(service, index, now):
    remotes, files = server_paths(service, server_headers(service))
    roots, unmapped = map_roots(remotes, service["path_map"])
    known, findings = set(), []
    for remote_file in files:
        local = to_local(remote_file, roots)
        if local is None:
            continue
        known.add(local)
        if local not in index:
            findings.append(("server_missing_file", local, 0, f"{service['name']} still lists this file"))
    for root in roots.values():
        for path, (mtime_ns, size) in index.items():
            if under(path, root) and path not in known and not is_extra(path) and now - mtime_ns / 1e9 > NEW_FILE_GRACE_SECONDS:
                findings.append((
                    "server_not_in_library", path, size,
                    f"In a {service['name']} library folder but not in {service['name']}. Check the "
                    "file name and permissions, or rescan the library. (Alternate versions of a movie can also show here.)",
                ))
    return findings, unmapped


def describe_error(exc):
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return f"HTTP {code}: the API key or token was rejected"
    if isinstance(code, int):
        return f"HTTP {code} {getattr(exc, 'reason', '')}".strip()
    reason = getattr(exc, "reason", None) or exc
    return f"{type(exc).__name__}: {reason}"[:300]


def load_index():
    with core.DB_LOCK, core.connect() as db:
        rows = db.execute("SELECT path,mtime_ns,size FROM files WHERE status!='missing'").fetchall()
    return {row["path"]: (row["mtime_ns"], row["size"]) for row in rows}


def run(now=None):
    """Check every configured app that has an API key. Returns findings not seen before."""
    if not RUN_LOCK.acquire(blocking=False):
        return []
    try:
        return _run(now or time.time())
    finally:
        RUN_LOCK.release()


def _run(now):
    services = [
        s for s in stack.load_config()
        if s["api_key"] and (s["type"] in ARR_TYPES or s["type"] in SERVER_TYPES)
    ]
    index = load_index()
    new, states = [], {}
    for service in services:
        state = {"name": service["name"], "type": service["type"], "checked_at": int(now), "error": None, "unmapped": []}
        try:
            if service["type"] in ARR_TYPES:
                findings, unmapped = check_arr(service, index)
            else:
                findings, unmapped = check_server(service, index, now)
            state["unmapped"] = unmapped
            state["findings"] = len(findings)
            with core.DB_LOCK, core.connect() as db:
                new += cleanup.save_findings(db, findings, CHECK_KINDS, source=service["name"])
        except Exception as exc:  # one app failing must not stop the others; keep its old findings
            state["error"] = describe_error(exc)
        states[service["name"]] = state
    with core.DB_LOCK, core.connect() as db:
        names = [s["name"] for s in services]
        if names:
            db.execute(
                f"DELETE FROM findings WHERE source IS NOT NULL AND source NOT IN ({','.join('?' * len(names))})",
                names,
            )
        else:
            db.execute("DELETE FROM findings WHERE source IS NOT NULL")
    STATE.update(services=states, last_run=int(now))
    return new


CHECK_KINDS = tuple(kind for kind in KINDS if kind != "unreadable")


def summary():
    return cleanup.group_summary(KINDS)


def snapshot():
    return {
        "last_run": STATE["last_run"],
        "services": list(STATE["services"].values()),
        "without_key": [
            s["name"] for s in stack.load_config()
            if s["type"] in ARR_TYPES | SERVER_TYPES and not s["api_key"]
        ],
        "summary": summary(),
    }


def library_roots(service):
    """The library or root folders an app reports, as the app sees them (a cheap call)."""
    base, token = service["url"], service["api_key"]
    if service["type"] in ARR_TYPES:
        return [r["path"] for r in fetch_json(f"{base}/api/v3/rootfolder", {"X-Api-Key": token})]
    if service["type"] == "plex":
        sections = fetch_json(f"{base}/library/sections", server_headers(service))["MediaContainer"].get("Directory", [])
        return [loc["path"] for s in sections if s.get("type") in ("movie", "show") for loc in s.get("Location", [])]
    prefix = "/emby" if service["type"] == "emby" else ""
    folders = fetch_json(f"{base}{prefix}/Library/VirtualFolders", server_headers(service))
    return [
        loc for folder in folders
        if (folder.get("CollectionType") or "mixed") in ("movies", "tvshows", "mixed", "homevideos")
        for loc in folder.get("Locations", [])
    ]


def test_service(service):
    """Check an app the way the dashboard's Test button needs: reachable, key accepted, folders mapped."""
    result = stack.check(service)
    out = {"up": result["up"], "code": result["code"], "ms": result["ms"], "error": result["error"],
           "warnings": result["warnings"], "key_checked": False, "key_error": None, "folders": []}
    if not result["up"] or not service["api_key"] or service["type"] not in ARR_TYPES | SERVER_TYPES:
        return out
    out["key_checked"] = True
    try:
        for remote in library_roots(service):
            out["folders"].append({"remote": remote, "local": map_root(remote, service["path_map"])})
    except Exception as exc:  # report the failure to the person testing, whatever it is
        out["key_error"] = describe_error(exc)
    return out
