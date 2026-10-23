import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

os.environ.setdefault("MG_USERNAME", "admin")
os.environ.setdefault("MG_PASSWORD", "test-password-longer-than-12")
os.environ.setdefault("MG_SESSION_SECRET", "test-session-secret-longer-than-32-characters")
os.environ["MG_BACKGROUND"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import cleanup, core, events, guardian, playback  # noqa: E402
from app.main import app  # noqa: E402

OLD = 1_000_000


def stream(codec_type, codec, lang=None, **extra):
    s = {"codec_type": codec_type, "codec_name": codec, **extra}
    if lang:
        s["tags"] = {"language": lang}
    return s


def probe(*streams, bit_rate=None):
    return {"v": playback.PROBE_VERSION, "streams": list(streams), "format": {"bit_rate": bit_rate} if bit_rate else {}}


def kinds(findings):
    return sorted(f[0] for f in findings)


def test_foreign_audio_needs_subtitles():
    jpn = probe(stream("video", "hevc", height=1080), stream("audio", "aac", "jpn"))
    assert kinds(playback.analyse("Anime/a.mkv", 1, jpn)) == ["foreign_audio_no_subs"]
    with_subs = probe(stream("video", "hevc"), stream("audio", "aac", "jpn"), stream("subtitle", "subrip", "eng"))
    assert playback.analyse("a.mkv", 1, with_subs) == []
    assert playback.analyse("Anime/a.mkv", 1, jpn, ["a.en.srt"]) == []
    assert playback.analyse("Anime/a.mkv", 1, jpn, ["a.srt"]) == []  # unlabeled: assume it's wanted
    assert kinds(playback.analyse("Anime/a.mkv", 1, jpn, ["a.fr.srt", "b.en.srt"])) == ["foreign_audio_no_subs"]
    undetermined = probe(stream("video", "hevc"), stream("audio", "aac", "und"))
    assert playback.analyse("a.mkv", 1, undetermined) == []
    english = probe(stream("video", "hevc"), stream("audio", "aac", "en"), stream("audio", "aac", "jpn"))
    assert playback.analyse("a.mkv", 1, english) == []


def test_bitmap_subtitles_only():
    pgs = probe(stream("video", "hevc"), stream("audio", "aac", "eng"), stream("subtitle", "hdmv_pgs_subtitle", "eng"))
    assert kinds(playback.analyse("a.mkv", 1, pgs)) == ["bitmap_subtitles_only"]
    assert playback.analyse("a.mkv", 1, pgs, ["a.en.srt"]) == []
    both = probe(stream("video", "hevc"), stream("audio", "aac", "eng"),
                 stream("subtitle", "hdmv_pgs_subtitle", "eng"), stream("subtitle", "subrip", "eng"))
    assert playback.analyse("a.mkv", 1, both) == []


def test_codecs_and_containers():
    old = probe(stream("video", "mpeg4"), stream("audio", "mp3", "eng"))
    found = playback.analyse("Movies/old.avi", 1000, old)
    assert kinds(found) == ["legacy_container", "reencode_candidate", "transcode_video"]
    assert [f[2] for f in found if f[0] == "reencode_candidate"] == [600]
    dts = probe(stream("video", "h264", height=1080), stream("audio", "dts", "eng"), stream("audio", "truehd", "eng"))
    found = playback.analyse("a.mkv", 1, dts)
    assert kinds(found) == ["transcode_audio"]
    assert "DTS, TrueHD" in found[0][3]
    cover_art = probe(stream("video", "hevc"), stream("video", "mjpeg"), stream("audio", "aac", "eng"))
    assert playback.analyse("a.mkv", 1, cover_art) == []


def test_reencode_thresholds():
    heavy = probe(stream("video", "h264", height=1080), stream("audio", "aac", "eng"), bit_rate="12000000")
    light = probe(stream("video", "h264", height=1080), stream("audio", "aac", "eng"), bit_rate="5000000")
    uhd = probe(stream("video", "h264", height=2160), stream("audio", "aac", "eng"), bit_rate="20000000")
    assert [f[2] for f in playback.analyse("a.mkv", 10_000, heavy)] == [5000]
    assert playback.analyse("a.mkv", 10_000, light) == []
    assert playback.analyse("a.mkv", 10_000, uhd) == []
    no_rate = probe(stream("video", "h264", height=1080), stream("audio", "aac", "eng"))
    assert playback.analyse("a.mkv", 10_000, no_rate) == []


def test_language_normalization():
    assert playback.normalize_language("EN") == "eng"
    assert playback.normalize_language("deu") == "ger"
    assert playback.sidecar_languages("Film.mkv", ["Film.eng.forced.srt", "Film.de.srt", "Other.en.srt", "Film.nfo"]) == {"eng", "ger"}


@pytest.fixture
def media(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        media.mkdir()
        monkeypatch.setattr(core, "MEDIA", media)
        monkeypatch.setattr(core, "BASE", root / "data")
        monkeypatch.setattr(core, "FILE_SLEEP", 0)
        monkeypatch.setattr(core, "MAX_PROBES", 10)
        monkeypatch.setattr(cleanup, "DOWNLOAD_ROOTS", [])
        core.init_db()
        yield media


def write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    os.utime(path, (OLD, OLD))


def fake_probe(results, calls):
    def run(path):
        calls.append(Path(path).name)
        return "ok", json.dumps(results[Path(path).name]), None
    return run


def found(kind):
    return sorted(f["path"] for f in cleanup.list_findings(kind))


def test_scan_flags_files_and_uses_sidecars(media, monkeypatch):
    jpn = probe(stream("video", "hevc"), stream("audio", "aac", "jpn"))
    results = {"Show.S01E01.mkv": jpn, "Show.S01E02.mkv": jpn}
    calls = []
    monkeypatch.setattr(core, "probe_file", fake_probe(results, calls))
    write(media / "Anime/Show/Show.S01E01.mkv")
    write(media / "Anime/Show/Show.S01E02.mkv")
    write(media / "Anime/Show/Show.S01E02.en.srt")
    summary = guardian.run_scan()
    assert found("foreign_audio_no_subs") == ["Anime/Show/Show.S01E01.mkv"]
    assert "playback_found" in [e["kind"] for e in events.list_events()]
    assert summary["probed"] == 2
    guardian.run_scan()
    assert len(calls) == 2  # current-version results are reused


def test_old_probe_results_are_refreshed(media, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "probe_file", fake_probe(
        {"a.mkv": probe(stream("video", "mpeg2video"), stream("audio", "ac3", "eng"))}, calls,
    ))
    write(media / "Movies/a.mkv")
    st = (media / "Movies/a.mkv").stat()
    with core.connect() as db:
        db.execute(
            "INSERT INTO files(path,size,mtime_ns,checked_at,status,probe_json) VALUES (?,?,?,?,?,?)",
            ("Movies/a.mkv", st.st_size, st.st_mtime_ns, 1, "ok", json.dumps({"streams": []})),
        )
    guardian.run_scan()
    assert calls == ["a.mkv"]
    assert found("transcode_video") == ["Movies/a.mkv"]


def test_mute_file_and_folder(media, monkeypatch):
    jpn = probe(stream("video", "hevc"), stream("audio", "aac", "jpn"))
    monkeypatch.setattr(core, "probe_file", fake_probe({"a.mkv": jpn, "b.mkv": jpn, "c.mkv": jpn}, []))
    for rel in ("Anime/X/a.mkv", "Anime/Y/b.mkv", "Movies/c.mkv"):
        write(media / rel)
    guardian.run_scan()
    assert len(found("foreign_audio_no_subs")) == 3
    cleanup.mute("foreign_audio_no_subs", "Anime")
    assert found("foreign_audio_no_subs") == ["Movies/c.mkv"]
    summary = {g["kind"]: g["count"] for g in cleanup.group_summary(playback.KINDS)}
    assert summary["foreign_audio_no_subs"] == 1
    assert cleanup.drop_muted([("foreign_audio_no_subs", "Anime/Z/d.mkv", 0, ""), ("transcode_video", "Anime/a.mkv", 0, "")]) == [
        ("transcode_video", "Anime/a.mkv", 0, "")
    ]
    cleanup.mute("foreign_audio_no_subs", "Movies/c.mkv")
    assert found("foreign_audio_no_subs") == []
    cleanup.unmute("foreign_audio_no_subs", "Anime")
    assert len(found("foreign_audio_no_subs")) == 2


def test_mute_api(media):
    with TestClient(app) as client:
        headers = {"Origin": "http://testserver"}
        client.post("/auth/login", json={"username": "admin", "password": os.environ["MG_PASSWORD"]}, headers=headers)
        assert client.post("/api/findings/mute", json={"kind": "nope", "path": "x"}, headers=headers).status_code == 400
        assert client.post("/api/findings/mute", json={"kind": "sample_file", "path": "x"}).status_code == 403
        assert client.post("/api/findings/mute", json={"kind": "sample_file", "path": "/Movies/x/"}, headers=headers).status_code == 200
        assert client.get("/api/muted").json()[0]["path"] == "Movies/x"
        assert client.get("/api/playback").json()["languages"] == ["eng"]
        assert client.get("/api/findings?kind=transcode_audio").status_code == 200
        assert client.post("/api/findings/unmute", json={"kind": "sample_file", "path": "Movies/x"}, headers=headers).status_code == 200
        assert client.get("/api/muted").json() == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ffprobe(media):
    target = media / "Movies/real.mkv"
    target.parent.mkdir(parents=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "mpeg2video", "-c:a", "mp2",
         "-metadata:s:a:0", "language=jpn", str(target)],
        check=True,
    )
    status, probe_json, issue = core.probe_file(target)
    assert status == "ok", issue
    data = json.loads(probe_json)
    assert data["v"] == playback.PROBE_VERSION
    assert kinds(playback.analyse("Movies/real.mkv", 100, data)) == [
        "foreign_audio_no_subs", "reencode_candidate", "transcode_audio", "transcode_video",
    ]
