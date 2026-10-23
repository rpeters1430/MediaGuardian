import os
import tempfile
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

from fastapi.testclient import TestClient
from app import core  # noqa: E402
from app.main import app  # noqa: E402


def test_auth_and_health(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        monkeypatch.setattr(core, "BASE", Path(directory))
        with TestClient(app) as client:
            assert client.get("/health/ready").status_code == 200
            assert client.get("/api/status").status_code == 401
            assert client.post("/auth/login", json={"username":"admin","password":os.environ["MG_PASSWORD"]}, headers={"Origin":"http://testserver"}).status_code == 200
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/libraries").status_code == 200
            assert client.post("/api/scan").status_code == 403


def test_empty_and_missing_files(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        media.mkdir()
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        monkeypatch.setattr(core, "MAX_PROBES", 0)
        core.init_db()
        movie = media / "movie.mkv"
        movie.touch()
        (media / "other.mkv").touch()
        assert core.scan_once()
        assert core.stats()["statuses"]["empty"] == 2
        movie.unlink()
        summary = core.scan_once()
        assert summary["missing_marked"] == 1
        assert core.stats()["statuses"]["missing"] == 1


def test_skips_symlink_directories(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        media.mkdir()
        outside = root / "outside"
        outside.mkdir()
        (outside / "secret.mkv").touch()
        (media / "escape").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        core.init_db()
        assert core.scan_once()
        assert core.stats()["total_files"] == 0
