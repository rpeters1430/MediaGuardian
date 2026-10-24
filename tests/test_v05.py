import http.server
import json
import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

import pytest  # noqa: E402

from app import cleanup, core, crosscheck, events, guardian, stack  # noqa: E402

OLD = 1_000_000

ROUTES = {
    # Radarr
    "/radarr/api/v3/rootfolder": [{"path": "/data/media/movies"}],
    "/radarr/api/v3/movie": [
        {"title": "Film A", "year": 2020, "path": "/data/media/movies/Film A (2020)", "hasFile": True,
         "movieFile": {"path": "/data/media/movies/Film A (2020)/Film A.mkv"}},
        {"title": "Film B", "year": 2021, "path": "/data/media/movies/Film B (2021)", "hasFile": True,
         "movieFile": {"path": "/data/media/movies/Film B (2021)/Film B.mkv"}},
        {"title": "Wanted", "year": 2022, "path": "/data/media/movies/Wanted (2022)", "hasFile": False},
    ],
    "/radarr/api/v3/queue": {"records": [
        {"title": "Film.C.2023.1080p", "size": 123, "trackedDownloadStatus": "warning",
         "trackedDownloadState": "importBlocked",
         "statusMessages": [{"title": "x", "messages": ["No files found are eligible for import"]}]},
        {"title": "Downloading.Fine", "trackedDownloadStatus": "ok", "trackedDownloadState": "downloading"},
    ]},
    # Sonarr
    "/sonarr/api/v3/rootfolder": [{"path": "/tv"}],
    "/sonarr/api/v3/series": [
        {"id": 1, "title": "Show", "year": 2019, "path": "/tv/Show", "statistics": {"episodeFileCount": 3}},
        {"id": 2, "title": "Complete", "path": "/tv/Complete", "statistics": {"episodeFileCount": 2}},
        # Counts more files than are on disk, but every file it lists is there (e.g. a multi-part file).
        {"id": 3, "title": "Counted", "path": "/tv/Stray", "statistics": {"episodeFileCount": 5}},
    ],
    "/sonarr/api/v3/episodefile?seriesId=1": [
        {"path": "/tv/Show/Season 1/S01E01.mkv"}, {"path": "/tv/Show/Season 1/S01E02.mkv"},
        {"path": "/tv/Show/Season 1/S01E03.mkv"},
    ],
    "/sonarr/api/v3/episodefile?seriesId=3": [{"path": "/tv/Stray/S01E01.mkv"}],
    "/sonarr/api/v3/queue": {"records": []},
    # Jellyfin
    "/jellyfin/Library/VirtualFolders": [
        {"Name": "Movies", "CollectionType": "movies", "Locations": ["/media/Movies"]},
        {"Name": "Music", "CollectionType": "music", "Locations": ["/media/Music"]},
    ],
    "/jellyfin/Items": {"Items": [
        {"Path": "/media/Movies/Film A (2020)/Film A.mkv"},
        {"Path": "/media/Movies/Gone (2001)/Gone.mkv"},
        {"Name": "Virtual episode"},
    ]},
    # Plex
    "/plex/library/sections": {"MediaContainer": {"Directory": [
        {"key": "2", "type": "show", "Location": [{"path": "/data/tv"}]},
    ]}},
    "/plex/library/sections/2/all": {"MediaContainer": {"Metadata": [
        {"Media": [{"Part": [{"file": "/data/tv/Show/Season 1/S01E01.mkv"}]}]},
        {"Media": [{"Part": [{"file": "/data/tv/Complete/S01E01.mkv"}]}]},
    ]}},
}
KEYS = {"/radarr": ("X-Api-Key", "rk"), "/sonarr": ("X-Api-Key", "sk"),
        "/jellyfin": ("Authorization", 'MediaBrowser Token="jk"'), "/plex": ("X-Plex-Token", "pk")}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path if self.path in ROUTES else self.path.split("?")[0]
        prefix = "/" + path.split("/")[1]
        header, key = KEYS[prefix]
        if self.headers.get(header) != key:
            self.send_response(401)
            self.end_headers()
            return
        if path not in ROUTES:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(ROUTES[path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def write(path, old=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    if old:
        os.utime(path, (OLD, OLD))


@pytest.fixture
def env(monkeypatch, server):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        for rel in [
            "Movies/Film A (2020)/Film A.mkv",
            "Movies/Untracked (1999)/Untracked.mkv",
            "Movies/Film A (2020)/Featurettes/Making of.mkv",
            "TV/Show/Season 1/S01E01.mkv",
            "TV/Show/Season 1/S01E02.mkv",
            "TV/Show/Season 1/S01E02-trailer.mkv",
            "TV/Complete/S01E01.mkv",
            "TV/Stray/S01E01.mkv",
        ]:
            write(media / rel)
        write(media / "Movies/Brand New (2024)/Brand New.mkv", old=False)
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        monkeypatch.setattr(core, "MAX_PROBES", 0)
        monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [])
        monkeypatch.setattr(stack, "STATE", {"services": {}, "config_error": None, "config_mtime": None, "config": []})
        monkeypatch.setattr(crosscheck, "STATE", {"services": {}, "last_run": None})
        core.init_db()
        services = [
            {"name": "Radarr", "type": "radarr", "url": f"{server}/radarr", "api_key": "rk"},
            {"name": "Sonarr", "type": "sonarr", "url": f"{server}/sonarr", "api_key": "sk"},
            {"name": "Jellyfin", "type": "jellyfin", "url": f"{server}/jellyfin", "api_key": "jk"},
            {"name": "Plex", "type": "plex", "url": f"{server}/plex", "api_key": "pk"},
        ]
        (core.BASE / "services.json").write_text(json.dumps(services))
        yield media


def found(kind):
    return sorted(f["path"] for f in cleanup.list_findings(kind))


def test_path_mapping(env):
    assert crosscheck.map_root("/data/media/movies", {}) == "Movies"
    assert crosscheck.map_root("/tv", {}) == "TV"
    assert crosscheck.map_root("/nowhere/at/all", {}) is None
    assert crosscheck.map_root("/data/media/anything", {"/data/media": ""}) == "anything"
    assert crosscheck.map_root("/data/media", {"/data/media": ""}) == ""
    roots = {"/data/media/movies": "Movies"}
    assert crosscheck.to_local("/data/media/movies/A/A.mkv", roots) == "Movies/A/A.mkv"
    assert crosscheck.to_local("/data/media/moviesX/A.mkv", roots) is None
    assert crosscheck.ancestors("a/b/c.mkv") == ["", "a", "a/b"]


def test_full_crosscheck(env):
    guardian.run_scan()
    # Show: the exact missing episode. Complete: the episodefile lookup fails, so the count is used.
    # Counted: its count is off but every listed file exists, so nothing is reported.
    assert found("arr_missing_file") == ["Movies/Film B (2021)/Film B.mkv", "TV/Complete", "TV/Show/Season 1/S01E03.mkv"]
    assert found("arr_stuck_import") == ["Radarr: Film.C.2023.1080p"]
    assert "No files found are eligible" in cleanup.list_findings("arr_stuck_import")[0]["detail"]
    # Radarr manages Film A and Film B; Untracked and Brand New aren't Radarr movies. Sonarr: Stray.
    assert found("arr_not_tracked") == ["Movies/Brand New (2024)", "Movies/Untracked (1999)"]
    assert found("server_missing_file") == ["Movies/Gone (2001)/Gone.mkv"]
    # Jellyfin covers Movies, Plex covers TV. New files, extras and trailers are skipped.
    assert found("server_not_in_library") == [
        "Movies/Untracked (1999)/Untracked.mkv", "TV/Show/Season 1/S01E02.mkv", "TV/Stray/S01E01.mkv",
    ]
    states = {s["name"]: s for s in crosscheck.snapshot()["services"]}
    assert all(s["error"] is None for s in states.values())
    kinds = [e["kind"] for e in events.list_events()]
    assert {"arr_missing_file", "server_missing_file", "arr_stuck_import", "server_not_in_library"} <= set(kinds)


def test_failing_app_keeps_its_findings(env):
    guardian.run_scan()
    before = found("server_missing_file")
    services = json.loads((core.BASE / "services.json").read_text())
    for s in services:
        if s["name"] == "Jellyfin":
            s["api_key"] = "wrong"
    (core.BASE / "services.json").write_text(json.dumps(services))
    os.utime(core.BASE / "services.json", (OLD + 10, OLD + 10))
    guardian.run_crosscheck()
    assert found("server_missing_file") == before
    state = {s["name"]: s for s in crosscheck.snapshot()["services"]}["Jellyfin"]
    assert "401" in state["error"]
    assert "crosscheck_failed" in [e["kind"] for e in events.list_events()]


def test_removed_app_drops_its_findings(env):
    guardian.run_scan()
    services = [s for s in json.loads((core.BASE / "services.json").read_text()) if s["name"] != "Radarr"]
    (core.BASE / "services.json").write_text(json.dumps(services))
    os.utime(core.BASE / "services.json", (OLD + 20, OLD + 20))
    guardian.run_crosscheck()
    assert found("arr_stuck_import") == []
    assert found("arr_missing_file") == ["TV/Complete", "TV/Show/Season 1/S01E03.mkv"]


def test_unmapped_roots_are_reported(env):
    ROUTES["/sonarr/api/v3/rootfolder"].append({"path": "/elsewhere/anime"})
    try:
        guardian.run_crosscheck()
    finally:
        ROUTES["/sonarr/api/v3/rootfolder"].pop()
    state = {s["name"]: s for s in crosscheck.snapshot()["services"]}["Sonarr"]
    assert state["unmapped"] == ["/elsewhere/anime"]


def test_keyless_apps_listed(env):
    services = json.loads((core.BASE / "services.json").read_text())
    services.append({"name": "Emby", "type": "emby", "url": "http://127.0.0.1:9"})
    (core.BASE / "services.json").write_text(json.dumps(services))
    assert crosscheck.snapshot()["without_key"] == ["Emby"]


def test_crosscheck_due(monkeypatch):
    monkeypatch.setattr(guardian, "CROSSCHECK_MINUTES", 60)
    monkeypatch.setitem(core.STATE, "last_finished", None)
    monkeypatch.setitem(crosscheck.STATE, "last_run", None)
    assert not guardian.crosscheck_due(10_000)  # wait for the first scan
    monkeypatch.setitem(core.STATE, "last_finished", 1)
    assert guardian.crosscheck_due(10_000)
    monkeypatch.setitem(crosscheck.STATE, "last_run", 9_000)
    assert not guardian.crosscheck_due(10_000)
    assert guardian.crosscheck_due(9_000 + 3600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_files_are_reported(env):
    secret = env / "Movies/Film A (2020)/Film A.mkv"
    secret.chmod(0)
    try:
        summary = guardian.run_scan()
    finally:
        secret.chmod(0o644)
    assert found("unreadable") == ["Movies/Film A (2020)/Film A.mkv"]
    assert any(f[0] == "unreadable" for f in summary["new_findings"])
