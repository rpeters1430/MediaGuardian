"""Playback and efficiency flags derived from each file's ffprobe result."""
import json
import os
from pathlib import PurePosixPath

PROBE_VERSION = 2

ISO_639_1 = {
    "en": "eng", "fr": "fre", "de": "ger", "es": "spa", "it": "ita", "ja": "jpn", "ko": "kor",
    "zh": "chi", "pt": "por", "ru": "rus", "nl": "dut", "sv": "swe", "no": "nor", "da": "dan",
    "fi": "fin", "pl": "pol", "hi": "hin", "ar": "ara", "tr": "tur", "he": "heb",
}
# ISO 639-2 has two codes for some languages (bibliographic and terminology).
SYNONYMS = {"fra": "fre", "deu": "ger", "zho": "chi", "nld": "dut", "nob": "nor", "nno": "nor"}
UNKNOWN_LANGUAGES = {"", "und", "unk", "mis", "mul", "zxx", "qaa"}


def normalize_language(code):
    code = (code or "").strip().lower()
    code = ISO_639_1.get(code, code)
    return SYNONYMS.get(code, code)


def _csv(name, default):
    return {item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip()}


LANGUAGES = {normalize_language(code) for code in _csv("MG_LANGUAGES", "eng")}
DIRECT_VIDEO = _csv("MG_DIRECT_PLAY_VIDEO", "h264,hevc,av1,vp9")
DIRECT_AUDIO = _csv("MG_DIRECT_PLAY_AUDIO", "aac,ac3,eac3,mp3,opus,flac,vorbis")
BITMAP_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
LEGACY_CONTAINERS = {".avi", ".wmv", ".mpg", ".mpeg"}
CODEC_NAMES = {
    "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4 Part 2 (XviD/DivX)", "msmpeg4v3": "DivX 3",
    "vc1": "VC-1", "wmv3": "WMV", "h264": "H.264", "hevc": "HEVC", "dts": "DTS", "truehd": "TrueHD",
    "pcm_s16le": "PCM", "pcm_s24le": "PCM", "pcm_bluray": "PCM", "mlp": "MLP",
}
# Above these bitrates (Mbps) an H.264 file is a good candidate for an HEVC/AV1 re-encode.
H264_HEAVY_MBPS = ((2160, 25), (1080, 8), (720, 4), (0, 2))
OLD_CODECS = {"mpeg2video", "mpeg4", "msmpeg4v3", "vc1", "wmv3"}

KINDS = {
    "foreign_audio_no_subs": "Foreign-language audio without subtitles",
    "bitmap_subtitles_only": "Only image-based subtitles (forces transcoding)",
    "transcode_video": "Video codec most devices can't play directly",
    "transcode_audio": "No audio track most devices can play directly",
    "legacy_container": "Old container format",
    "reencode_candidate": "Could shrink with Shrinkarr",
}

FFPROBE_ENTRIES = (
    "format=duration,format_name,bit_rate"
    ":stream=index,codec_name,codec_type,width,height,bit_rate,channels"
    ":stream_tags=language:stream_disposition=forced"
)


def codec_name(codec):
    return CODEC_NAMES.get(codec, codec.upper() if codec else "unknown")


def needs_reprobe(probe_json):
    if not probe_json:
        return False
    try:
        return json.loads(probe_json).get("v") != PROBE_VERSION
    except ValueError:
        return True


def sidecar_languages(video_name, sidecar_names):
    """Languages of external subtitle files that belong to this video. None means unlabeled."""
    stem = PurePosixPath(video_name).stem
    languages = set()
    for name in sidecar_names:
        sub = PurePosixPath(name)
        if sub.suffix.lower() not in (".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx"):
            continue
        if not sub.stem.startswith(stem):
            continue
        tags = [normalize_language(t) for t in sub.stem[len(stem):].split(".") if t]
        known = [t for t in tags if len(t) == 3]
        languages.update(known or [None])
    return languages


def analyse(relpath, size, probe, sidecars=()):
    """Findings for one probed file. probe is the parsed ffprobe JSON."""
    streams = probe.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    findings = []

    def add(kind, detail, bytes_=0):
        findings.append((kind, relpath, bytes_, detail))

    audio_langs = {normalize_language((s.get("tags") or {}).get("language")) for s in audio}
    if audio and audio_langs.isdisjoint(LANGUAGES) and not (audio_langs & UNKNOWN_LANGUAGES):
        internal = {normalize_language((s.get("tags") or {}).get("language")) for s in subs}
        external = sidecar_languages(PurePosixPath(relpath).name, sidecars)
        if internal.isdisjoint(LANGUAGES | UNKNOWN_LANGUAGES) and not (external & (LANGUAGES | {None})):
            add(
                "foreign_audio_no_subs",
                f"Audio is {', '.join(sorted(audio_langs))} only, with no {'/'.join(sorted(LANGUAGES))} subtitles",
            )

    wanted_subs = [s for s in subs if normalize_language((s.get("tags") or {}).get("language")) in LANGUAGES]
    external = sidecar_languages(PurePosixPath(relpath).name, sidecars)
    if wanted_subs and all(s.get("codec_name") in BITMAP_SUBTITLES for s in wanted_subs) and not external:
        add(
            "bitmap_subtitles_only",
            f"Subtitles are {codec_name(wanted_subs[0].get('codec_name'))} images. Turning them on makes most "
            "clients burn them in with a full video transcode; a text (SRT) subtitle avoids that",
        )

    if video:
        codec = video[0].get("codec_name")
        if codec not in DIRECT_VIDEO:
            add("transcode_video", f"{codec_name(codec)} video: most TVs, phones and browsers need the server to transcode it")
    if audio and not any(s.get("codec_name") in DIRECT_AUDIO for s in audio):
        codecs = ", ".join(sorted({codec_name(s.get("codec_name")) for s in audio}))
        add("transcode_audio", f"Only {codecs} audio: many TVs, phones and browsers need it transcoded")

    suffix = PurePosixPath(relpath).suffix.lower()
    if suffix in LEGACY_CONTAINERS:
        add("legacy_container", f"{suffix[1:].upper()} files lack modern subtitle and seeking support; a remux to MKV or MP4 helps")

    if video:
        saving = reencode_saving(video[0], probe.get("format") or {}, size)
        if saving:
            add("reencode_candidate", saving[1], saving[0])
    return findings


def reencode_saving(stream, fmt, size):
    """(estimated bytes saved, detail) for files worth re-encoding, else None."""
    codec = stream.get("codec_name")
    if codec in OLD_CODECS:
        return int(size * 0.6), f"{codec_name(codec)} video; HEVC or AV1 is typically 60% smaller"
    if codec != "h264":
        return None
    try:
        mbps = int(fmt.get("bit_rate") or stream.get("bit_rate")) / 1_000_000
    except (TypeError, ValueError):
        return None
    height = stream.get("height") or 0
    limit = next(limit for min_height, limit in H264_HEAVY_MBPS if height >= min_height)
    if mbps <= limit:
        return None
    return int(size * 0.5), f"H.264 at {mbps:.1f} Mbps for {height}p; HEVC or AV1 is typically about half the size"
