import http.server
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cleanup, core, events, guardian, playback, settings, stack  # noqa: E402
from app.main import app  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def data(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "media" / "Movies").mkdir(parents=True)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "MEDIA", root / "media")
        monkeypatch.setattr(stack, "STATE", {"services": {}, "config_error": None, "config_mtime": None, "config": []})
        for key in settings.SPEC:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("MG_SERVICES", raising=False)
        core.init_db()
        yield root
        settings.apply()  # put module values back to defaults for other tests


@pytest.fixture
def client(data):
    with TestClient(app) as c:
        c.post("/auth/login", json={"username": "admin", "password": os.environ["MG_PASSWORD"]}, headers=ORIGIN)
        yield c


def by_key(items):
    return {item["key"]: item for item in items}


def test_precedence_saved_then_environment_then_default(data, monkeypatch):
    assert settings.resolve()["MG_SCAN_INTERVAL_MINUTES"] == (360, "default")
    monkeypatch.setenv("MG_SCAN_INTERVAL_MINUTES", "120")
    assert settings.resolve()["MG_SCAN_INTERVAL_MINUTES"] == (120, "environment")
    assert settings.save({"MG_SCAN_INTERVAL_MINUTES": "45"}) == {}
    assert settings.resolve()["MG_SCAN_INTERVAL_MINUTES"] == (45, "saved")
    assert guardian.SCAN_INTERVAL_MINUTES == 45
    assert settings.save({"MG_SCAN_INTERVAL_MINUTES": None}) == {}
    assert settings.resolve()["MG_SCAN_INTERVAL_MINUTES"] == (120, "environment")
    assert guardian.SCAN_INTERVAL_MINUTES == 120
    monkeypatch.setenv("MG_SCAN_INTERVAL_MINUTES", "lots")
    assert settings.resolve()["MG_SCAN_INTERVAL_MINUTES"] == (360, "default")


def test_validation_rejects_bad_values_and_saves_nothing(data):
    errors = settings.save({"MG_SCAN_WINDOW": "5-5", "MG_FILE_SLEEP": "9", "MG_DOWNLOADS": "downloads", "MG_NTFY_URL": "ntfy.sh/x", "MG_LANGUAGES": "spa"})
    assert set(errors) == {"MG_SCAN_WINDOW", "MG_FILE_SLEEP", "MG_DOWNLOADS", "MG_NTFY_URL"}
    assert "between 0 and 1" in errors["MG_FILE_SLEEP"]
    assert settings.load_saved() == {}
    assert settings.save({"MG_NOPE": 1}) == {"MG_NOPE": "Unknown setting"}


def test_apply_reaches_every_module(data):
    assert settings.save({
        "MG_SCAN_WINDOW": "1-7", "MG_EXCLUDE": "Extras, Backups", "MG_DOWNLOADS": "/downloads,/torrents",
        "MG_LANGUAGES": "en,spa", "MG_DIRECT_PLAY_AUDIO": "AAC", "MG_MAX_PROBES_PER_SCAN": 7,
        "MG_SERVICE_INTERVAL": 30, "MG_NTFY_URL": "https://ntfy.sh/topic",
    }) == {}
    assert guardian.WINDOW == (1, 7)
    assert {"extras", "backups", "lost+found"} <= core.EXCLUDE_DIRS
    assert cleanup.DOWNLOAD_ROOTS == [Path("/downloads"), Path("/torrents")]
    assert playback.LANGUAGES == {"eng", "spa"}
    assert playback.DIRECT_AUDIO == {"aac"}
    assert core.MAX_PROBES == 7 and stack.SERVICE_INTERVAL == 30
    assert events.NTFY_URL == "https://ntfy.sh/topic"


def test_secrets_are_masked_and_kept(data):
    assert settings.save({"MG_NTFY_TOKEN": "tk_secret", "MG_DISCORD_WEBHOOK": "https://discord.com/api/webhooks/1/x"}) == {}
    described = by_key(settings.describe())
    assert described["MG_NTFY_TOKEN"]["value"] == "" and described["MG_NTFY_TOKEN"]["is_set"]
    assert "tk_secret" not in json.dumps(settings.describe())
    assert settings.save({"MG_NTFY_TOKEN": ""}) == {}  # blank keeps it
    assert events.NTFY_TOKEN == "tk_secret"
    assert settings.save({"MG_NTFY_TOKEN": None}) == {}  # reset clears it
    assert events.NTFY_TOKEN == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_settings_and_apps_files_are_private(data):
    settings.save({"MG_NTFY_TOKEN": "x"})
    settings.save_services([{"name": "Sonarr", "type": "sonarr", "url": "http://s:8989", "api_key": "k"}])
    for path in (settings.settings_file(), stack.services_file()):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_services_round_trip_keeps_keys(data):
    settings.save_services([
        {"name": "Sonarr", "type": "sonarr", "url": "http://s:8989/", "api_key": "sonarr-key",
         "path_map": {"/data/media": ""}},
        {"name": "Jellyfin", "type": "jellyfin", "url": "http://j:8096"},
    ])
    listed = settings.list_services()
    assert listed[0] == {"name": "Sonarr", "type": "sonarr", "url": "http://s:8989", "has_key": True, "path_map": {"/data/media": ""}}
    assert "sonarr-key" not in json.dumps(listed)
    # Rename without re-entering the key
    settings.save_services([{"name": "Sonarr 4K", "type": "sonarr", "url": "http://s:8989", "keep_key_from": "Sonarr"}])
    assert stack.load_config()[0]["api_key"] == "sonarr-key"


def test_services_validation(data, monkeypatch):
    with pytest.raises(ValueError, match="Two apps"):
        settings.save_services([{"name": "A", "url": "http://a"}, {"name": "A", "url": "http://b"}])
    with pytest.raises(ValueError, match="http://"):
        settings.save_services([{"name": "A", "url": "a.local"}])
    with pytest.raises(ValueError, match="Unknown type"):
        settings.save_services([{"name": "A", "url": "http://a", "type": "sickbeard"}])
    assert not stack.services_file().exists()
    monkeypatch.setenv("MG_SERVICES", "[]")
    with pytest.raises(ValueError, match="MG_SERVICES"):
        settings.save_services([])


def test_settings_api(client):
    body = client.get("/api/settings").json()
    keys = by_key(body["settings"])
    assert keys["MG_SCAN_INTERVAL_MINUTES"]["source"] == "default"
    assert body["services_editable"] is True and "sonarr" in body["service_types"]
    assert client.post("/api/settings", json={"changes": {"MG_SCAN_INTERVAL_MINUTES": 90}}).status_code == 403
    bad = client.post("/api/settings", json={"changes": {"MG_SCAN_INTERVAL_MINUTES": -1}}, headers=ORIGIN)
    assert bad.status_code == 400 and "MG_SCAN_INTERVAL_MINUTES" in bad.json()["detail"]["errors"]
    ok = client.post("/api/settings", json={"changes": {"MG_SCAN_INTERVAL_MINUTES": 90}}, headers=ORIGIN)
    assert by_key(ok.json()["settings"])["MG_SCAN_INTERVAL_MINUTES"]["source"] == "saved"
    assert client.put("/api/settings/services", json={"services": [{"name": "X", "url": "nope"}]}, headers=ORIGIN).status_code == 400
    saved = client.put("/api/settings/services", json={"services": [{"name": "X", "type": "generic", "url": "http://x"}]}, headers=ORIGIN)
    assert saved.json()["services"][0]["name"] == "X"
    assert client.post("/api/settings/test-notification", headers=ORIGIN).status_code == 400


def test_notification_test_reports_failures(client, monkeypatch):
    client.post("/api/settings", json={"changes": {"MG_NTFY_URL": "http://127.0.0.1:9/topic"}}, headers=ORIGIN)
    result = client.post("/api/settings/test-notification", headers=ORIGIN).json()
    assert result["sent"] is False and result["error"]
    sent = []
    monkeypatch.setattr(events, "_post", lambda url, body, headers: sent.append(url))
    assert client.post("/api/settings/test-notification", headers=ORIGIN).json() == {"sent": True, "error": None}
    assert sent == ["http://127.0.0.1:9/topic"]


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ping":
            body = b"{}"
        elif path == "/api/v3/health":
            body = b"[]"
        elif path == "/api/v3/rootfolder" and self.headers.get("X-Api-Key") == "good":
            body = json.dumps([{"path": "/data/media/movies"}, {"path": "/nowhere"}]).encode()
        else:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


def test_test_service_shows_key_and_folder_mapping(client):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        good = client.post("/api/settings/test-service", json={"service": {"name": "Radarr", "type": "radarr", "url": url, "api_key": "good"}}, headers=ORIGIN).json()
        assert good["up"] and good["key_checked"] and good["key_error"] is None
        assert good["folders"] == [{"remote": "/data/media/movies", "local": "Movies"}, {"remote": "/nowhere", "local": None}]
        bad = client.post("/api/settings/test-service", json={"service": {"name": "Radarr", "type": "radarr", "url": url, "api_key": "bad"}}, headers=ORIGIN).json()
        assert "rejected" in bad["key_error"]
        # A saved key is used when the form leaves it blank
        client.put("/api/settings/services", json={"services": [{"name": "Radarr", "type": "radarr", "url": url, "api_key": "good"}]}, headers=ORIGIN)
        reused = client.post("/api/settings/test-service", json={"service": {"name": "Radarr", "type": "radarr", "url": url}}, headers=ORIGIN).json()
        assert reused["key_error"] is None and reused["folders"]
        invalid = client.post("/api/settings/test-service", json={"service": {"name": "R", "url": "bad"}}, headers=ORIGIN)
        assert invalid.status_code == 400
    finally:
        httpd.shutdown()


def test_shrinkarr_temp_artwork_is_not_an_interrupted_replacement():
    assert cleanup.junk_kind("Colony (2026) Bluray-1080p.shrinkarr.tmp-poster.jpg") == "shrinkarr_metadata"
    assert cleanup.junk_kind("Film.shrinkarr.tmp.nfo") == "shrinkarr_metadata"
    assert cleanup.junk_kind("Film.shrinkarr.tmp.mkv") == "shrinkarr_leftover"
    assert cleanup.junk_kind("Film.mkv.shrinkarr.bak") == "shrinkarr_leftover"
    assert cleanup.junk_kind("Film.mkv.shrinkarr.bak.txn") == "shrinkarr_leftover"


def test_media_server_auth_headers():
    from app import crosscheck
    jellyfin = {"type": "jellyfin", "api_key": "k"}
    assert crosscheck.server_headers(jellyfin) == {"Authorization": 'MediaBrowser Token="k"'}
    assert crosscheck.server_headers({"type": "emby", "api_key": "k"}) == {"X-Emby-Token": "k"}
    assert crosscheck.server_headers({"type": "plex", "api_key": "k"}) == {"X-Plex-Token": "k"}
