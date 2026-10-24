import os
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

import pytest  # noqa: E402

from app import core, events, guardian, stack  # noqa: E402


@pytest.fixture
def media(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        media.mkdir()
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        monkeypatch.setattr(core, "MAX_PROBES", 0)
        monkeypatch.setattr(core, "MISSING_GUARD_MIN", 3)
        monkeypatch.setattr(stack, "STATE", {"services": {}, "config_error": None, "config_mtime": None, "config": []})
        core.init_db()
        yield media


def make(media, *names):
    for name in names:
        path = media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


def kinds():
    return [event["kind"] for event in events.list_events()]


def test_empty_root_is_held_then_confirmed(media):
    make(media, "Movies/a.mkv", "Movies/b.mkv")
    guardian.run_scan()
    for path in media.rglob("*.mkv"):
        path.unlink()
    held = guardian.run_scan()
    assert held["missing_held"] == 2 and held["missing_marked"] == 0
    assert core.stats()["statuses"].get("missing") is None
    assert "mass_missing" in kinds()
    confirmed = guardian.run_scan()
    assert confirmed["missing_marked"] == 2
    assert "files_missing" in kinds()


def test_mass_disappearance_is_held(media):
    make(media, *[f"TV/show{i}.mkv" for i in range(10)])
    guardian.run_scan()
    for i in range(5):
        (media / f"TV/show{i}.mkv").unlink()
    assert guardian.run_scan()["missing_held"] == 5


def test_files_reappearing_clear_the_hold(media):
    make(media, *[f"TV/show{i}.mkv" for i in range(10)])
    guardian.run_scan()
    hidden = media.parent / "hidden"
    (media / "TV").rename(hidden)
    assert guardian.run_scan()["missing_held"] == 10
    hidden.rename(media / "TV")
    summary = guardian.run_scan()
    assert summary["missing_held"] == 0 and summary["missing_marked"] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_unreadable_folder_does_not_mark_its_files_missing(media):
    make(media, "Movies/a.mkv", "Movies/b.mkv", "TV/c.mkv", "TV/d.mkv")
    guardian.run_scan()
    (media / "TV").chmod(0)
    try:
        (media / "Movies/a.mkv").unlink()
        summary = guardian.run_scan()
    finally:
        (media / "TV").chmod(0o755)
    assert summary["errors"] == 1
    assert summary["missing_marked"] == 1
    assert core.stats()["statuses"]["missing"] == 1


def test_skips_housekeeping_folders_and_temp_files(media):
    make(
        media, "Movies/a.mkv", "#recycle/old.mkv", "Movies/@eaDir/thumb.mp4",
        "Movies/.Trash-1000/x.mkv", "Movies/b.shrinkarr.tmp.mkv", "Movies/._a.mkv",
    )
    assert guardian.run_scan()["seen"] == 1


def test_previously_indexed_excluded_paths_are_dropped(media):
    make(media, "Movies/a.mkv")
    guardian.run_scan()
    with core.connect() as db:
        db.execute(
            "INSERT INTO files(path,size,mtime_ns,checked_at,status) VALUES ('#recycle/old.mkv',1,1,1,'ok')"
        )
    summary = guardian.run_scan()
    assert summary["missing_marked"] == 0
    assert core.stats()["total_files"] == 1


def test_recently_modified_files_are_not_probed(media, monkeypatch):
    monkeypatch.setattr(core, "MAX_PROBES", 5)
    probed = []
    monkeypatch.setattr(core, "probe_file", lambda path: probed.append(path) or ("ok", "{}", None))
    make(media, "Movies/new.mkv")
    old = media / "Movies/old.mkv"
    make(media, "Movies/old.mkv")
    os.utime(old, (1, 1))
    guardian.run_scan()
    assert [Path(p).name for p in probed] == ["old.mkv"]


def test_scan_history_is_recorded(media):
    make(media, "Movies/a.mkv")
    guardian.run_scan("scheduled")
    history = core.list_scans()
    assert history[0]["trigger"] == "scheduled"
    assert history[0]["new_files"] == 1
    assert core.last_scan_finished() == history[0]["finished_at"]


def test_scan_window():
    assert guardian.parse_window("") is None
    assert guardian.in_window(3, (1, 7)) and not guardian.in_window(7, (1, 7))
    assert guardian.in_window(23, (22, 6)) and guardian.in_window(2, (22, 6))
    assert not guardian.in_window(12, (22, 6))
    with pytest.raises(ValueError):
        guardian.parse_window("5-5")


def test_scan_due(monkeypatch):
    monkeypatch.setattr(guardian, "SCAN_INTERVAL_MINUTES", 60)
    monkeypatch.setattr(guardian, "WINDOW", None)
    monkeypatch.setitem(core.STATE, "running", False)
    monkeypatch.setitem(core.STATE, "last_finished", None)
    assert not guardian.scan_due(1000, 1000)
    assert guardian.scan_due(1000 + guardian.STARTUP_DELAY, 1000)
    monkeypatch.setitem(core.STATE, "last_finished", 5000)
    assert not guardian.scan_due(5000 + 3599, 0)
    assert guardian.scan_due(5000 + 3600, 0)


def test_acknowledge_events(media):
    events.record("warning", "test", "hello")
    assert len(events.list_events()) == 1
    events.acknowledge_all()
    assert events.list_events() == []
    assert len(events.list_events(include_acknowledged=True)) == 1


def test_notification_sent_for_warnings(media, monkeypatch):
    sent = threading.Event()
    calls = []
    monkeypatch.setattr(events, "NTFY_URL", "http://ntfy.local/media")
    monkeypatch.setattr(events, "_post", lambda url, body, headers: calls.append((url, body)) or sent.set())
    events.record("info", "quiet", "not sent")
    events.record("warning", "loud", "sent")
    assert sent.wait(2)
    assert calls == [("http://ntfy.local/media", b"sent")]


def test_stack_config_and_transitions(media, monkeypatch):
    (core.BASE / "services.json").write_text(
        '[{"name":"Sonarr","url":"http://user:pw@sonarr:8989/","type":"sonarr","api_key":"k"},'
        '{"name":"Jellyfin","url":"http://jellyfin:8096","type":"jellyfin"}]'
    )
    results = {"Sonarr": {"up": True, "code": 200, "error": None, "warnings": ["Indexers: all down"], "ms": 3}}
    down = {"up": False, "code": None, "error": "timed out", "warnings": [], "ms": 5000}
    monkeypatch.setattr(stack, "check", lambda service: dict(results.get(service["name"], down)))
    stack.check_all()
    snap = stack.snapshot()
    assert [s["name"] for s in snap["services"]] == ["Sonarr", "Jellyfin"]
    assert snap["services"][0]["url"] == "http://sonarr:8989"
    assert snap["services"][1]["status"] == "unstable"
    assert "service_warning" in kinds() and "service_down" not in kinds()
    stack.check_all()
    assert stack.snapshot()["services"][1]["status"] == "down"
    assert kinds().count("service_down") == 1 and kinds().count("service_warning") == 1
    results["Jellyfin"] = {"up": True, "code": 200, "error": None, "warnings": [], "ms": 2}
    stack.check_all()
    assert stack.snapshot()["services"][1]["status"] == "up"
    assert "service_up" in kinds()
    assert "api_key" not in str(stack.snapshot())


def test_stack_rejects_bad_config(media):
    (core.BASE / "services.json").write_text('[{"name":"X","url":"ftp://x","type":"sonarr"}]')
    stack.load_config()
    assert "http(s) url" in stack.snapshot()["config_error"]


def test_services_from_environment_override_the_file(media, monkeypatch):
    (core.BASE / "services.json").write_text('[{"name":"FromFile","url":"http://a:1","type":"generic"}]')
    monkeypatch.setenv("MG_SERVICES", '[{"name":"FromEnv","url":"http://b:2","type":"sonarr","api_key":"k"}]')
    assert [s["name"] for s in stack.load_config()] == ["FromEnv"]
    assert stack.snapshot()["config_file"] == "MG_SERVICES"
    monkeypatch.setenv("MG_SERVICES", '{"not": "a list"}')
    stack.load_config()
    assert "MG_SERVICES must contain a list" in stack.snapshot()["config_error"]
    monkeypatch.delenv("MG_SERVICES")
    assert [s["name"] for s in stack.load_config()] == ["FromFile"]
