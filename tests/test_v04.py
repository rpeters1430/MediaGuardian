import os
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

import pytest  # noqa: E402

from app import cleanup, core, events, guardian  # noqa: E402

OLD = 1_000_000  # an mtime far in the past


@pytest.fixture
def roots(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media, downloads = root / "media", root / "downloads"
        media.mkdir()
        downloads.mkdir()
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        monkeypatch.setattr(core, "MAX_PROBES", 0)
        monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [downloads])
        monkeypatch.setattr(cleanup, "MIN_MATCH_BYTES", 1)
        monkeypatch.setattr(cleanup, "UNIMPORTED_DAYS", 1)
        core.init_db()
        yield media, downloads


def write(path, data=b"video", old=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if old:
        os.utime(path, (OLD, OLD))
    return path


def found(kind):
    return sorted(f["path"] for f in cleanup.list_findings(kind))


def test_hardlinked_download_is_fine_and_copy_is_reported(roots):
    media, downloads = roots
    linked = write(downloads / "tv/Show.S01E01.mkv", b"a" * 1000)
    (media / "TV/Show").mkdir(parents=True)
    os.link(linked, media / "TV/Show/S01E01.mkv")
    copied = write(downloads / "movies/Film.2020.mkv", b"b" * 2000)
    write(media / "Movies/Film (2020)/Film.mkv", copied.read_bytes())
    guardian.run_scan()
    assert found("copied_download") == [str(copied)]
    assert found("download_not_in_library") == []


def test_same_size_different_content_is_not_a_copy(roots):
    media, downloads = roots
    other = write(downloads / "a.mkv", b"x" * 500)
    write(media / "Movies/b.mkv", b"y" * 500)
    guardian.run_scan()
    assert found("copied_download") == []
    assert found("download_not_in_library") == [str(other)]


def test_download_not_in_library(roots):
    media, downloads = roots
    old = write(downloads / "old.mkv", b"z" * 300)
    write(downloads / "fresh.mkv", b"q" * 301, old=False)
    write(downloads / "Film.sample.mkv", b"s" * 302)
    write(media / "Movies/a.mkv")
    guardian.run_scan()
    assert found("download_not_in_library") == [str(old)]


def test_download_folder_inside_media_is_not_a_library(roots, monkeypatch):
    media, _ = roots
    inner = media / "Downloads"
    write(inner / "x.mkv", b"d" * 100)
    write(media / "Movies/a.mkv")
    monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [])
    guardian.run_scan()
    assert core.stats()["total_files"] == 2
    monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [inner])
    summary = guardian.run_scan()
    assert summary["missing_marked"] == 0 and summary["missing_held"] == 0
    assert core.stats()["total_files"] == 1
    assert found("download_not_in_library") == [str(inner / "x.mkv")]


def test_leftovers_samples_and_folders(roots):
    media, _ = roots
    write(media / "Movies/A/A.mkv.shrinkarr.bak")
    write(media / "Movies/A/A.mkv.shrinkarr.bak.txn")
    write(media / "Movies/A/A.mkv")
    write(media / "Movies/B/B.shrinkarr.tmp.mkv", old=False)  # still being written
    write(media / "Movies/B/B.mkv")
    write(media / "Movies/C/C.mkv.part")
    write(media / "Movies/D/D-sample.mkv")
    write(media / "Movies/D/D.mkv")
    write(media / "Movies/Gone/Gone.nfo")
    write(media / "Movies/Gone/Gone.en.srt")
    (media / "Movies/Empty/@eaDir").mkdir(parents=True)
    write(media / "Music/Album/01.flac")
    write(media / "Music/Album/album.nfo")
    summary = guardian.run_scan()
    assert found("shrinkarr_leftover") == ["Movies/A/A.mkv.shrinkarr.bak", "Movies/A/A.mkv.shrinkarr.bak.txn"]
    assert found("incomplete_download") == ["Movies/C/C.mkv.part"]
    assert found("sample_file") == ["Movies/D/D-sample.mkv"]
    assert found("empty_folder") == ["Movies/Empty"]
    assert found("no_video_folder") == ["Movies/Gone"]
    kinds = [e["kind"] for e in events.list_events()]
    assert "shrinkarr_leftover" in kinds and "cleanup_found" in kinds
    assert len(summary["new_findings"]) == 6
    assert guardian.run_scan()["new_findings"] == []  # already known


def test_findings_clear_when_fixed(roots):
    media, _ = roots
    junk = write(media / "Movies/A/A.mkv.part")
    write(media / "Movies/A/A.mkv")
    guardian.run_scan()
    assert found("incomplete_download")
    junk.unlink()
    guardian.run_scan()
    assert found("incomplete_download") == []


def test_big_shrink_alerts(roots):
    media, _ = roots
    film = write(media / "Movies/A.mkv", b"x" * 1000)
    guardian.run_scan()
    write(film, b"x" * 50)
    os.utime(film, (OLD + 5, OLD + 5))
    summary = guardian.run_scan()
    assert summary["shrunk"] == [("Movies/A.mkv", 1000, 50)]
    assert "file_shrunk" in [e["kind"] for e in events.list_events()]


def test_forecast():
    day = 86400
    assert cleanup.forecast([(0, 1000, 500)])["days_left"] is None
    result = cleanup.forecast([(0, 1000, 500), (day, 1000, 490), (2 * day, 1000, 480)])
    assert result["growth_per_day"] == 10
    assert result["days_left"] == 48.0
    assert result["used_percent"] == 52.0
    shrinking = cleanup.forecast([(0, 1000, 500), (2 * day, 1000, 600)])
    assert shrinking["days_left"] is None


def test_disk_alert_fires_once_and_recovers(roots, monkeypatch):
    usage = {"free": 50}
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(1000, 1000 - usage["free"], usage["free"]))
    monkeypatch.setattr(cleanup, "CAPACITY_SAMPLE_SECONDS", 0)
    guardian.check_capacity()
    guardian.check_capacity()
    kinds = [e["kind"] for e in events.list_events()]
    assert kinds.count("disk_low") == 1
    usage["free"] = 500
    guardian.check_capacity()
    assert "disk_ok" in [e["kind"] for e in events.list_events()]


def test_volumes_match(roots, monkeypatch):
    assert cleanup.volumes_match() is True
    monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [])
    assert cleanup.volumes_match() is None


def test_forecast_ignores_samples_from_before_a_resize():
    day = 86400
    result = cleanup.forecast([(0, 500, 100), (day, 1000, 500), (2 * day, 1000, 480), (3 * day, 1000, 460)])
    assert result["growth_per_day"] == 20
