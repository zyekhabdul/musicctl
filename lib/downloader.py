#!/usr/bin/env python3
"""Production concurrent download engine for musicctl."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import catalog
import common
import providers

AUDIO_EXTS = {".opus", ".mp3", ".m4a", ".flac", ".ogg", ".wav", ".webm"}
VALID_QUALITIES = ("small", "cd", "hires")
VALID_FORMATS = ("auto", "flac", "opus", "m4a", "mp3")
DEFAULT_TEMPLATE = "{artist}/{album}/{track:02d} - {title}"
BAD_PATH_CHARS_RE = re.compile(r'[/\\\x00:*?"<>|\t\r\n]+')
SPOTIFY_URL_RE = re.compile(r"^https?://open\.spotify\.com/(track|album|playlist)/([A-Za-z0-9]+)", re.I)


parse_spotify_html = common.parse_spotify_html


def _fetch_spotify_tracks(kind: str, sp_id: str, url: str) -> list[dict[str, Any]]:
    parsed = common.fetch_spotify_tracks(kind, sp_id, url)
    return [
        catalog.make_entity(source="spotify", id=f"sp-{sp_id}-{idx}", type="song", title=t, artist=a)
        for idx, (t, a) in enumerate(parsed, start=1)
    ]


def sanitize_segment(text: str) -> str:
    """Sanitize a filename or directory segment safely."""
    cleaned = BAD_PATH_CHARS_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned in ("", ".", ".."):
        return ""
    return cleaned


def render_dest_path(download_dir: Path, template: str, track: dict[str, Any], ext: str) -> Path:
    """Render destination audio path from template and clamp inside download_dir."""
    tpl = (template or "").strip() or DEFAULT_TEMPLATE
    artist = sanitize_segment(track.get("artist") or "")
    album = sanitize_segment(track.get("album") or "")
    title = sanitize_segment(track.get("title") or track.get("id") or "track")
    track_num = max(0, int(track.get("track_num") or 0))
    year = max(0, int(track.get("year") or 0))
    tid = sanitize_segment(track.get("id") or "")
    source = sanitize_segment(track.get("source") or "ytm")

    values = {
        "artist": artist,
        "album": album,
        "title": title,
        "track": track_num,
        "year": year,
        "id": tid,
        "source": source,
    }

    raw_segs = [s for s in tpl.replace("\\", "/").split("/") if s.strip()]
    if not raw_segs:
        raw_segs = ["{title}"]

    dir_parts: list[str] = []
    for seg in raw_segs[:-1]:
        try:
            rendered = seg.format(**values)
        except Exception:
            rendered = seg
        clean = sanitize_segment(rendered)
        if clean:
            dir_parts.append(clean)

    last_seg = raw_segs[-1]
    try:
        rendered_name = last_seg.format(**values)
    except Exception:
        rendered_name = title or tid or "track"
    if track_num == 0:
        rendered_name = re.sub(r"^0+(?:\s*[-._]\s*|\s+)", "", rendered_name)
    fname = sanitize_segment(rendered_name) or title or tid or "track"

    clean_ext = ext.lstrip(".").lower() or "m4a"
    dest = (download_dir / Path(*dir_parts) / f"{fname}.{clean_ext}") if dir_parts else (download_dir / f"{fname}.{clean_ext}")

    base_res = download_dir.resolve()
    dest_res = dest.resolve()
    if base_res != dest_res and base_res not in dest_res.parents:
        raise ValueError(f"Destination path escapes download_dir: {dest}")
    return dest


def archive_key(track: dict[str, Any]) -> str:
    """Compute canonical key for download-archive.tsv."""
    src = str(track.get("source") or "ytm").lower()
    tid = str(track.get("id") or "")
    if src == "ytm" and common.VIDEO_ID_RE.match(tid):
        return tid
    return f"{src}:{tid}" if tid else ""


def load_archive(archive_path: Path) -> dict[tuple[str, str, str], str]:
    """Load download archive keyed by (track_key, quality, format) -> dest_path."""
    entries: dict[tuple[str, str, str], str] = {}
    if not archive_path.is_file():
        return entries
    for line in archive_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0]:
            entries[(parts[0], parts[1], parts[2])] = parts[3]
    return entries


@contextlib.contextmanager
def _file_lock(target_path: Path, thread_lock: threading.Lock | None = None) -> Iterator[None]:
    """Serialize in-process threads and cross-process writers via persistent sibling flock."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_path.with_name(f".{target_path.name}.lock")
    if thread_lock is not None:
        thread_lock.acquire()
    try:
        with open(lock_path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        if thread_lock is not None:
            thread_lock.release()


def record_archive(archive_path: Path, lock: threading.Lock, tkey: str, quality: str, fmt: str, dest: Path) -> None:
    """Thread-safe and process-safe atomic upsert into download-archive.tsv."""
    if not tkey:
        return
    with _file_lock(archive_path, lock):
        existing: list[str] = []
        if archive_path.is_file():
            for ln in archive_path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = ln.split("\t")
                if len(parts) >= 3 and (parts[0], parts[1], parts[2]) == (tkey, quality, fmt):
                    continue
                if ln.strip():
                    existing.append(ln)
        dest_clean = str(dest).replace("\t", " ").replace("\n", " ")
        existing.append(f"{tkey}\t{quality}\t{fmt}\t{dest_clean}\t{int(time.time())}")
        tmp = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
        tmp.replace(archive_path)


def prepare_square_cover(
    track_id: str,
    cover_url: str,
    cache_art_dir: Path,
    tmp_dir: Path,
) -> Path | None:
    """Download/load cover art and center-crop rectangular images to 1:1 square."""
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(track_id or "")) or "cover"
    cache_art_dir.mkdir(parents=True, exist_ok=True)
    art_file = cache_art_dir / f"{safe_id}.jpg"

    dl_url = (cover_url or "").strip()
    if not dl_url and common.VIDEO_ID_RE.match(track_id or ""):
        dl_url = f"https://i.ytimg.com/vi/{track_id}/hqdefault.jpg"

    if (not art_file.is_file() or art_file.stat().st_size == 0) and dl_url:
        try:
            req = urllib.request.Request(dl_url, headers={"User-Agent": common.USER_AGENT})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = resp.read()
            if data:
                tmp_art = cache_art_dir / f".{safe_id}.{threading.get_ident()}.tmp"
                tmp_art.write_bytes(data)
                tmp_art.replace(art_file)
        except urllib.error.HTTPError as exc:
            exc.close()
        except Exception:
            pass

    if not art_file.is_file() or art_file.stat().st_size == 0:
        return None

    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        return art_file

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(art_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
        obj = json.loads(probe.stdout or "{}")
        streams = obj.get("streams") or []
        if not streams:
            return None
        w = int(streams[0].get("width") or 0)
        h = int(streams[0].get("height") or 0)
        if w <= 0 or h <= 0:
            return None
        if w == h:
            return art_file
        sq_out = tmp_dir / f".sq_{safe_id}_{threading.get_ident()}.jpg"
        crop = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(art_file),
                "-vf",
                "crop='min(iw,ih)':'min(iw,ih)'",
                str(sq_out),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
        )
        if crop.returncode == 0 and sq_out.is_file() and sq_out.stat().st_size > 0:
            return sq_out
    except Exception:
        return art_file
    return art_file


def _default_lyrics_fetcher(title: str, artist: str, duration: float) -> str:
    res = common.fetch_lyrics(title, artist=artist, duration=duration)
    if not res:
        return ""
    return str(res.get("synced_text") or res.get("plain_text") or "")


def fetch_and_write_lrc(
    dest_audio: Path,
    title: str,
    artist: str = "",
    duration: float = 0.0,
    fetcher: Callable[[str, str, float], str] | None = None,
) -> Path | None:
    """Fetch lyrics and write atomic .lrc sidecar next to dest_audio."""
    fn = fetcher or _default_lyrics_fetcher
    body = (fn(title, artist, duration) or "").strip()
    if not body:
        return None
    lrc_path = dest_audio.with_suffix(".lrc")
    lrc_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lrc_path.with_name(f".{lrc_path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(lrc_path)
    return lrc_path


def compute_replaygain_tags(audio_path: Path) -> dict[str, str]:
    """Compute EBU R128 track ReplayGain tags via ffmpeg."""
    if not shutil.which("ffmpeg"):
        return {}
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-af",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30.0,
        )
    except Exception:
        return {}
    err = proc.stderr or ""
    m_i = re.search(r"Integrated loudness:\s*[\r\n]+\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS", err, re.I)
    m_p = re.search(r"True peak:\s*[\r\n]+\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS", err, re.I)
    gain_db = 0.0
    if m_i and m_i.group(1).lower() != "-inf":
        lufs = float(m_i.group(1))
        gain_db = max(-30.0, min(30.0, -18.0 - lufs))
    peak_lin = 0.0
    if m_p and m_p.group(1).lower() != "-inf":
        dbfs = float(m_p.group(1))
        peak_lin = 10.0 ** (dbfs / 20.0)
    return {
        "REPLAYGAIN_TRACK_GAIN": f"{gain_db:+.2f} dB",
        "REPLAYGAIN_TRACK_PEAK": f"{peak_lin:.6f}",
    }


def _build_metadata_args(track: dict[str, Any], rg_tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    title = str(track.get("title") or "").strip()
    artist = str(track.get("artist") or "").strip()
    album = str(track.get("album") or "").strip()
    isrc = str(track.get("isrc") or "").strip().upper()
    tnum = int(track.get("track_num") or 0)
    year = int(track.get("year") or 0)
    if title:
        args.extend(["-metadata", f"title={title}"])
    if artist:
        args.extend(["-metadata", f"artist={artist}"])
    if album:
        args.extend(["-metadata", f"album={album}"])
    if tnum > 0:
        args.extend(["-metadata", f"track={tnum}"])
    if year > 0:
        args.extend(["-metadata", f"date={year}"])
    if isrc:
        args.extend(["-metadata", f"isrc={isrc}"])
    for k, v in rg_tags.items():
        args.extend(["-metadata", f"{k}={v}"])
    return args


def finalize_audio(
    src_file: Path,
    dest_path: Path,
    *,
    quality: str = "cd",
    fmt: str = "auto",
    track: dict[str, Any],
    cover_file: Path | None = None,
    replaygain: bool = False,
    src_codec: str = "",
) -> tuple[Path, str]:
    """Remux (zero generation-loss) or transcode, embed tags/cover, and move atomically."""
    if not src_codec:
        info = providers.validate_audio_file(src_file, 0.0, False, False)
        src_codec = info["codec"] if info else ""

    if fmt == "auto":
        if quality == "small" and providers.is_lossless_codec(src_codec):
            target_ext = "opus"
            audio_codec_args = ["-c:a", "libopus", "-b:a", "128k"]
            codec_lbl = f"opus (from {src_codec})"
        else:
            target_ext = providers.ext_for_codec(src_codec)
            audio_codec_args = ["-c:a", "copy"]
            codec_lbl = src_codec or target_ext
    else:
        target_ext = fmt
        same_codec = (
            (fmt == "flac" and src_codec == "flac")
            or (fmt == "opus" and src_codec == "opus")
            or (fmt == "mp3" and src_codec == "mp3")
            or (fmt == "m4a" and src_codec in ("aac", "alac"))
        )
        if same_codec:
            audio_codec_args = ["-c:a", "copy"]
            codec_lbl = src_codec
        elif fmt == "flac":
            audio_codec_args = ["-c:a", "flac"]
            codec_lbl = f"flac (from {src_codec})" if src_codec else "flac"
        elif fmt == "opus":
            br = "96k" if quality == "small" else "128k"
            audio_codec_args = ["-c:a", "libopus", "-b:a", br]
            codec_lbl = f"opus (from {src_codec})" if src_codec else "opus"
        elif fmt == "m4a":
            br = "128k" if quality == "small" else "192k"
            audio_codec_args = ["-c:a", "aac", "-b:a", br]
            codec_lbl = f"aac (from {src_codec})" if src_codec else "aac"
        elif fmt == "mp3":
            q_args = ["-b:a", "128k"] if quality == "small" else ["-q:a", "2"]
            audio_codec_args = ["-c:a", "libmp3lame", *q_args]
            codec_lbl = f"mp3 (from {src_codec})" if src_codec else "mp3"
        else:
            audio_codec_args = ["-c:a", "copy"]
            codec_lbl = src_codec or fmt

    final_dest = dest_path.with_suffix(f".{target_ext}")
    final_dest.parent.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        shutil.copyfile(src_file, final_dest)
        return final_dest, codec_lbl

    rg_tags = compute_replaygain_tags(src_file) if replaygain else {}
    meta_args = _build_metadata_args(track, rg_tags)
    can_cover = (
        cover_file is not None
        and cover_file.is_file()
        and cover_file.stat().st_size > 0
        and target_ext in ("flac", "m4a", "mp3")
    )
    movflags = ["-movflags", "+faststart+use_metadata_tags"] if target_ext == "m4a" else []

    fd, tmp_name = tempfile.mkstemp(prefix=".out.", suffix=f".{target_ext}", dir=str(final_dest.parent))
    os.close(fd)
    tmp_out = Path(tmp_name)
    try:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_file)]
        if can_cover:
            cmd.extend(["-i", str(cover_file), "-map", "0:a", "-map", "1:v", *audio_codec_args, "-c:v", "copy", "-disposition:v", "attached_pic"])
        else:
            cmd.extend(["-map", "0:a", *audio_codec_args])
        cmd.extend(meta_args)
        cmd.extend(movflags)
        cmd.append(str(tmp_out))

        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120.0)
        if res.returncode != 0 and can_cover:
            fb_cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src_file), "-map", "0:a", *audio_codec_args, *meta_args, *movflags, str(tmp_out)]
            res = subprocess.run(fb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120.0)

        if res.returncode != 0 or not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg failed finalizing {final_dest.name}")

        os.replace(tmp_out, final_dest)
        return final_dest, codec_lbl
    finally:
        tmp_out.unlink(missing_ok=True)


def _ytm_format_for_quality(quality: str) -> str:
    if quality == "small":
        return "249/250/139/bestaudio[abr<=96]/bestaudio/best"
    return "774/251/141/bestaudio[acodec=opus]/bestaudio/best"


def _parse_meta_fields(meta: str) -> tuple[str, str, int]:
    parsed = common.parse_meta_fields(meta)
    return parsed["artist"], parsed["album"], int(parsed["duration"])


def _resolve_current_track(sock_path: str) -> dict[str, Any]:
    client = common.MpvClient(sock_path, timeout=1.0)
    snap = client.batch_status_snapshot()
    if not snap or snap.get("idle"):
        raise RuntimeError("No track currently playing")
    ok, resp_line = client.raw_request('["get_property","path"]', timeout=1.0)
    path = ""
    if ok and resp_line:
        try:
            path = str(json.loads(resp_line).get("data") or "")
        except Exception:
            path = ""
    vid = common.extract_video_id(path) if path else ""
    raw_title = str(snap.get("title") or "")
    title = common.normalize_track_title("" if raw_title == vid else raw_title) or vid
    return catalog.make_entity(
        source="ytm" if common.VIDEO_ID_RE.match(vid) else "local",
        id=vid or path,
        type="song",
        isrc=str(snap.get("isrc") or ""),
        title=title,
        artist=str(snap.get("artist") or ""),
        album=str(snap.get("album") or ""),
        track_num=int(snap.get("track_num") or 0),
        year=int(snap.get("year") or 0),
        duration=int(round(float(snap.get("duration") or 0))),
    )


def _is_explicit_ref(tok: str) -> bool:
    return bool(
        common.VIDEO_ID_RE.match(tok)
        or catalog.parse_ref(tok) is not None
        or re.match(r"^https?://", tok, re.I)
        or tok.startswith("pl:")
    )


def resolve_download_targets(targets: list[str], data_dir: Path, sock_path: str) -> list[dict[str, Any]]:
    """Resolve CLI targets (current, pl:<name>, catalog refs, URLs, queries) into tracks."""
    if not targets or targets == ["current"]:
        return [_resolve_current_track(sock_path)]

    clean_tokens = [t.strip() for t in targets if t and t.strip()]
    if len(clean_tokens) > 1 and all(_is_explicit_ref(tok) for tok in clean_tokens):
        batch_rows: list[dict[str, Any]] = []
        for tok in clean_tokens:
            batch_rows.extend(resolve_download_targets([tok], data_dir, sock_path))
        return batch_rows

    joined = " ".join(targets).strip()
    if joined.startswith("pl:"):
        pl_name = joined[3:].strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", pl_name):
            raise ValueError(f"Invalid playlist name: {pl_name}")
        pl_path = data_dir / "playlists" / f"{pl_name}.tsv"
        if not pl_path.is_file():
            raise ValueError(f"Playlist not found: {pl_name}")
        rows: list[dict[str, Any]] = []
        for line in pl_path.read_text(encoding="utf-8", errors="replace").splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0]:
                artist, album, dur = _parse_meta_fields(cols[2] if len(cols) > 2 else "")
                rows.append(
                    catalog.make_entity(
                        source="ytm",
                        id=cols[0].strip(),
                        type="song",
                        title=common.normalize_track_title(cols[1].strip()),
                        artist=artist,
                        album=album,
                        duration=dur,
                    )
                )
        return rows

    if Path(joined).is_file():
        p = Path(joined).resolve()
        return [catalog.make_entity(source="local", id=str(p), type="song", title=p.stem)]

    if catalog.parse_ref(joined) is not None:
        res = catalog.browse_catalog(joined)
        ent = res.get("entity") or {}
        if ent.get("type") == "song":
            return [ent]
        return [it for it in (res.get("items") or []) if it.get("type") == "song"]

    m_sp = SPOTIFY_URL_RE.match(joined)
    if m_sp:
        sp_rows = _fetch_spotify_tracks(m_sp.group(1).lower(), m_sp.group(2), joined)
        if not sp_rows:
            raise RuntimeError(f"Failed to extract tracks from Spotify URL: {joined}")
        return sp_rows

    if re.match(r"^https?://(?:[^/]+\.)?(?:soundcloud\.com|bandcamp\.com)/", joined, re.I):
        proc = subprocess.run(
            [
                "yt-dlp",
                "--ignore-config",
                "--flat-playlist",
                "--no-update",
                "--print",
                "%(webpage_url|url)s\t%(title)s\t%(artist,uploader)s\t%(album)s\t%(duration)s",
                joined,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20.0,
        )
        rows = []
        for ln in (proc.stdout or "").splitlines():
            cols = ln.split("\t")
            if cols and cols[0]:
                rows.append(
                    catalog.make_entity(
                        source="ytdlp",
                        id=cols[0],
                        type="song",
                        title=cols[1] if len(cols) > 1 and cols[1] != "NA" else cols[0],
                        artist=cols[2] if len(cols) > 2 and cols[2] != "NA" else "",
                        album=cols[3] if len(cols) > 3 and cols[3] != "NA" else "",
                        duration=int(float(cols[4])) if len(cols) > 4 and cols[4] not in ("", "NA", "None") else 0,
                    )
                )
        return rows or [catalog.make_entity(source="ytdlp", id=joined, type="song", title=joined)]

    if re.match(r"^https?://", joined, re.I):
        vid = common.extract_video_id(joined)
        return [catalog.make_entity(source="ytm", id=vid, type="song", title=vid)]

    if common.VIDEO_ID_RE.match(joined):
        return [catalog.make_entity(source="ytm", id=joined, type="song", title=joined)]

    found = catalog.search_ytm(joined, "songs", limit=1)
    if found:
        return [found[0]]
    raise RuntimeError(f"No results found for: {joined}")


def _ytdlp_fallback_download(
    track: dict[str, Any],
    quality: str,
    tmp_dir: Path,
    cookies: str = "",
    sponsorblock: bool = False,
) -> Path | None:
    tid = str(track.get("id") or "")
    src = str(track.get("source") or "ytm")
    url = ""
    if src == "ytdlp" or tid.startswith("http"):
        url = tid
    elif src == "ytm" and common.VIDEO_ID_RE.match(tid):
        url = f"https://www.youtube.com/watch?v={tid}"
    else:
        q = f"{track.get('artist') or ''} {track.get('title') or ''}".strip()
        if q:
            found = catalog.search_ytm(q, "songs", limit=1)
            if found:
                url = f"https://www.youtube.com/watch?v={found[0]['id']}"
    if not url:
        return None

    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-update",
        "-q",
        "-f",
        _ytm_format_for_quality(quality),
    ]
    if cookies:
        if Path(cookies).is_file():
            cmd.extend(["--cookies", cookies])
        else:
            cmd.extend(["--cookies-from-browser", cookies])
    if sponsorblock:
        cmd.extend(["--sponsorblock-remove", "music_offtopic"])
    stem = f"ytdlp_{threading.get_ident()}_{int(time.time() * 1000)}"
    cmd.extend(["-x", "--audio-format", "best", "--audio-quality", "0", "-o", str(tmp_dir / f"{stem}.%(ext)s"), url])

    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300.0)
    if proc.returncode != 0:
        return None
    for p in tmp_dir.glob(f"{stem}.*"):
        if p.is_file() and p.stat().st_size > 0 and p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


def _jobs_file(data_dir: Path) -> Path:
    return data_dir / "dl-jobs.json"


def _sync_state_file(data_dir: Path) -> Path:
    return data_dir / "sync-state.json"


def _sync_source_key(targets: list[str]) -> str:
    return " ".join(targets).strip()


def load_sync_state(data_dir: Path) -> dict[str, list[str]]:
    """Load per-source sync ownership manifest {source_key: [dest_path, ...]}."""
    sf = _sync_state_file(data_dir)
    if not sf.is_file():
        return {}
    try:
        raw = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, list):
            out[k] = [str(p) for p in v if isinstance(p, str) and p.strip()]
    return out


def save_sync_state(data_dir: Path, lock: threading.Lock, state: dict[str, list[str]]) -> None:
    """Persist per-source sync ownership manifest atomically with cross-process lock."""
    sf = _sync_state_file(data_dir)
    with _file_lock(sf, lock):
        fd, tmp_name = tempfile.mkstemp(prefix=".sync-state.", suffix=".tmp", dir=str(sf.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_name, sf)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def _load_jobs(data_dir: Path) -> list[dict[str, Any]]:
    jf = _jobs_file(data_dir)
    if not jf.is_file():
        return []
    try:
        raw = json.loads(jf.read_text(encoding="utf-8"))
        return [dict(x) for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    except Exception:
        return []


def _save_jobs_unlocked(jf: Path, jobs: list[dict[str, Any]]) -> None:
    jf.parent.mkdir(parents=True, exist_ok=True)
    trimmed = jobs[-20:]
    fd, tmp_name = tempfile.mkstemp(prefix=".dl-jobs.", suffix=".tmp", dir=str(jf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(trimmed, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_name, jf)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _save_jobs(data_dir: Path, lock: threading.Lock, jobs: list[dict[str, Any]]) -> None:
    jf = _jobs_file(data_dir)
    with _file_lock(jf, lock):
        _save_jobs_unlocked(jf, jobs)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _upsert_job(data_dir: Path, lock: threading.Lock, entry: dict[str, Any]) -> None:
    jf = _jobs_file(data_dir)
    with _file_lock(jf, lock):
        jobs = [j for j in _load_jobs(data_dir) if j.get("job_id") != entry.get("job_id")]
        jobs.append(entry)
        _save_jobs_unlocked(jf, jobs)


def _cmd_dl_status(data_dir: Path, as_json: bool = False) -> int:
    jf = _jobs_file(data_dir)
    with _file_lock(jf):
        jobs = _load_jobs(data_dir)
        dirty = False
        for j in jobs:
            if j.get("status") == "running" and not _pid_alive(int(j.get("pid") or 0)):
                j["status"] = "stale"
                dirty = True
        if dirty:
            _save_jobs_unlocked(jf, jobs)
    if as_json:
        print(json.dumps(jobs, ensure_ascii=False))
        return 0
    if not jobs:
        print("[IDLE] No download jobs recorded")
        return 0
    for j in jobs:
        print(f"{j.get('job_id')}  {j.get('status')}  {j.get('done', 0)}/{j.get('total', 0)}  {j.get('quality', '')}/{j.get('format', '')}")
    return 0


def _cmd_dl_cancel(data_dir: Path, target_id: str = "all") -> int:
    jf = _jobs_file(data_dir)
    cancelled = 0
    with _file_lock(jf):
        jobs = _load_jobs(data_dir)
        for j in jobs:
            if j.get("status") != "running":
                continue
            jid = str(j.get("job_id") or "")
            if target_id not in ("", "all") and jid != target_id:
                continue
            pid = int(j.get("pid") or 0)
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    cancelled += 1
                except OSError:
                    pass
            j["status"] = "cancelled"
            j["updated_at"] = int(time.time())
        _save_jobs_unlocked(jf, jobs)
    print(f"[OK] Cancelled {cancelled} download job(s)")
    return 0


def _process_one_track(
    track: dict[str, Any],
    *,
    quality: str,
    fmt: str,
    template: str,
    download_dir: Path,
    cache_art_dir: Path,
    providers_dir: str,
    hooks_dir: str,
    archive_path: Path,
    archive_map: dict[tuple[str, str, str], str],
    archive_lock: threading.Lock,
    lrc: bool,
    replaygain: bool,
    force: bool,
    dry_run: bool,
    cookies: str,
    sponsorblock: bool,
    job_id: str = "",
    emit_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, Path | None, str]:
    tkey = archive_key(track)
    clean_title = common.normalize_track_title(str(track.get("title") or track.get("id") or "track"))
    track_norm = dict(track)
    track_norm["title"] = clean_title

    def _emit(stage: str, pct: int, status: str, dest: Path | None = None, error: str = "") -> None:
        if emit_event is not None:
            emit_event(
                {
                    "job_id": job_id,
                    "title": clean_title,
                    "stage": stage,
                    "pct": pct,
                    "status": status,
                    "dest": str(dest) if dest is not None else "",
                    "error": error,
                }
            )

    if cancel_event is not None and cancel_event.is_set():
        _emit("cancelled", 100, "cancelled", error="cancelled")
        return "failed", None, f"[WARN] Cancelled: {clean_title}"

    if not force and tkey and (tkey, quality, fmt) in archive_map:
        arch_dest = Path(archive_map[(tkey, quality, fmt)])
        if arch_dest.is_file() and arch_dest.stat().st_size > 0:
            _emit("skipped", 100, "skipped", dest=arch_dest)
            return "skipped", arch_dest, f"[SKIP] Cached in archive: {arch_dest}"

    planned_ext = "opus" if (fmt == "auto" and quality == "small") else ("flac" if fmt == "auto" else fmt)
    planned_dest = render_dest_path(download_dir, template, track_norm, planned_ext)
    if dry_run:
        _emit("dry_run", 100, "done", dest=planned_dest)
        return "dry_run", planned_dest, f"[DRY-RUN] {planned_dest} ({quality}, format={fmt})"

    _emit("queued", 0, "running", dest=planned_dest)
    with tempfile.TemporaryDirectory(prefix="musicctl-dl-") as tmp_str:
        tmp_dir = Path(tmp_str)
        _emit("fetching", 25, "running", dest=planned_dest)
        req = providers.build_provider_request(
            "download",
            quality,
            str(track_norm.get("id") or ""),
            clean_title,
            str(track_norm.get("artist") or ""),
            str(track_norm.get("album") or ""),
            int(track_norm.get("duration") or 0),
            str(track_norm.get("isrc") or ""),
            int(track_norm.get("track_num") or 0),
            int(track_norm.get("year") or 0),
            source=str(track_norm.get("source") or "ytm"),
            fmt=fmt,
        )
        prov_hit = providers.fetch_validated_download(req, providers_dir, tmp_dir)
        prov_name = "ytm"
        src_codec = ""
        src_srate = 0
        src_bits = 0
        if prov_hit is not None:
            raw_file, info, cand = prov_hit
            prov_name = str(cand.get("provider") or "provider")
            src_codec = str(info.get("codec") or "")
            src_srate = int(info.get("sample_rate") or 0)
            src_bits = int(info.get("bit_depth") or 0)
        else:
            raw_dl = _ytdlp_fallback_download(track_norm, quality, tmp_dir, cookies=cookies, sponsorblock=sponsorblock)
            if raw_dl is None:
                _emit("failed", 100, "error", error="No audio source available")
                return "failed", None, f"[ERR] Failed to download: {clean_title}"
            raw_file = raw_dl
            info = providers.validate_audio_file(raw_file, 0.0, False, False) or {}
            src_codec = str(info.get("codec") or "")
            src_srate = int(info.get("sample_rate") or 0)
            src_bits = int(info.get("bit_depth") or 0)

        if cancel_event is not None and cancel_event.is_set():
            _emit("cancelled", 100, "cancelled", error="cancelled")
            return "failed", None, f"[WARN] Cancelled: {clean_title}"

        _emit("finalizing", 75, "running", dest=planned_dest)
        cover_file = prepare_square_cover(
            str(track_norm.get("id") or ""),
            str(track_norm.get("cover_url") or ""),
            cache_art_dir,
            tmp_dir,
        )
        target_dest = render_dest_path(download_dir, template, track_norm, providers.ext_for_codec(src_codec) if fmt == "auto" else fmt)
        final_dest, final_codec = finalize_audio(
            raw_file,
            target_dest,
            quality=quality,
            fmt=fmt,
            track=track_norm,
            cover_file=cover_file,
            replaygain=replaygain,
            src_codec=src_codec,
        )
        if lrc:
            fetch_and_write_lrc(
                final_dest,
                clean_title,
                str(track_norm.get("artist") or ""),
                float(track_norm.get("duration") or 0),
            )
        record_archive(archive_path, archive_lock, tkey, quality, fmt, final_dest)
        common.trigger_hooks(
            "on-download-complete",
            {
                "id": str(track_norm.get("id") or ""),
                "source": str(track_norm.get("source") or "ytm"),
                "title": clean_title,
                "artist": str(track_norm.get("artist") or ""),
                "album": str(track_norm.get("album") or ""),
                "isrc": str(track_norm.get("isrc") or ""),
                "quality": quality,
                "format": fmt,
                "codec": final_codec,
                "path": str(final_dest),
            },
            hooks_dir=hooks_dir,
            timeout=3.0,
        )
        _emit("done", 100, "done", dest=final_dest)
        bits_lbl = f", {src_bits}-bit" if src_bits > 0 else ""
        hz_lbl = f", {src_srate}Hz" if src_srate > 0 else ""
        return "downloaded", final_dest, f"[OK] Downloaded ({prov_name}, {final_codec}{hz_lbl}{bits_lbl}): {final_dest}"


def run_download(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musicctl dl")
    parser.add_argument("--quality", "-q", default="", help="small|cd|hires|ask")
    parser.add_argument("--format", "-f", dest="fmt", default="auto", choices=VALID_FORMATS)
    parser.add_argument("--jobs", "-j", type=int, default=1)
    parser.add_argument("--template", "-t", default="")
    parser.add_argument("--lrc", action="store_true")
    parser.add_argument("--replaygain", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("targets", nargs="*")

    args = parser.parse_args(argv)
    data_dir = Path(os.environ.get("MUSICCTL_DATA_DIR") or os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "musicctl"))

    targets = list(args.targets)
    if targets and targets[0] == "status":
        return _cmd_dl_status(data_dir, as_json=args.as_json)
    if targets and targets[0] == "cancel":
        tid = targets[1] if len(targets) > 1 else "all"
        return _cmd_dl_cancel(data_dir, target_id=tid)

    sync_mode = False
    if targets and targets[0] == "sync":
        sync_mode = True
        targets = targets[1:]
        if not targets:
            print("[ERR] Usage: musicctl dl sync <source> [--prune]", file=sys.stderr)
            return 1

    cache_dir = Path(os.environ.get("MUSICCTL_CACHE_DIR") or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "musicctl"))
    download_dir = Path(os.environ.get("MUSICCTL_DOWNLOAD_DIR") or os.path.expanduser("~/Music/musicctl"))
    providers_dir = os.environ.get("MUSICCTL_PROVIDERS_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "musicctl", "providers")
    hooks_dir = os.environ.get("MUSICCTL_HOOKS_DIR") or os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "musicctl", "hooks")
    sock_path = os.environ.get("MUSICCTL_SOCKET") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "musicctl.sock")
    cookies = os.environ.get("MUSICCTL_YTM_COOKIES") or ""
    sponsorblock = os.environ.get("MUSICCTL_SPONSORBLOCK", "0") == "1"

    quality = (args.quality or os.environ.get("MUSICCTL_QUALITY") or "cd").strip().lower()
    if quality == "ask":
        quality = "cd"
    if quality not in VALID_QUALITIES:
        print(f"[ERR] Unknown quality tier '{quality}' (valid: small, cd, hires, ask)", file=sys.stderr)
        return 1

    jobs = max(1, min(4, int(args.jobs)))
    template = (args.template or os.environ.get("MUSICCTL_DL_TEMPLATE") or DEFAULT_TEMPLATE).strip()

    try:
        tracks = resolve_download_targets(targets, data_dir, sock_path)
    except Exception as exc:
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1

    if not tracks and not sync_mode:
        print("[ERR] No tracks resolved for download", file=sys.stderr)
        return 1

    download_dir.mkdir(parents=True, exist_ok=True)
    archive_path = data_dir / "download-archive.tsv"
    archive_map = load_archive(archive_path)
    archive_lock = threading.Lock()
    jobs_lock = threading.Lock()
    print_lock = threading.Lock()
    cache_art_dir = cache_dir / "art"

    job_id = f"dl-{int(time.time())}-{os.getpid()}"
    cancel_event = threading.Event()
    job_record: dict[str, Any] = {
        "job_id": job_id,
        "pid": os.getpid(),
        "quality": quality,
        "format": args.fmt,
        "total": len(tracks),
        "done": 0,
        "failed": 0,
        "status": "running",
        "updated_at": int(time.time()),
    }
    _upsert_job(data_dir, jobs_lock, job_record)

    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        def _on_sigterm(_signum: int, _frame: Any) -> None:
            cancel_event.set()
        prev_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)

    def emit_jsonl(ev: dict[str, Any]) -> None:
        if not args.jsonl:
            return
        with print_lock:
            print(json.dumps(ev, ensure_ascii=False, separators=(",", ":")), flush=True)

    expected_paths: set[Path] = set()
    counts = {"downloaded": 0, "skipped": 0, "failed": 0, "dry_run": 0}

    def run_one(trk: dict[str, Any]) -> None:
        status, dest, msg = _process_one_track(
            trk,
            quality=quality,
            fmt=args.fmt,
            template=template,
            download_dir=download_dir,
            cache_art_dir=cache_art_dir,
            providers_dir=providers_dir,
            hooks_dir=hooks_dir,
            archive_path=archive_path,
            archive_map=archive_map,
            archive_lock=archive_lock,
            lrc=args.lrc,
            replaygain=args.replaygain,
            force=args.force,
            dry_run=args.dry_run,
            cookies=cookies,
            sponsorblock=sponsorblock,
            job_id=job_id,
            emit_event=emit_jsonl if args.jsonl else None,
            cancel_event=cancel_event,
        )
        with print_lock:
            counts[status] = counts.get(status, 0) + 1
            if dest is not None:
                expected_paths.add(dest.resolve())
            if not args.jsonl:
                print(msg, flush=True)

    try:
        if jobs == 1 or len(tracks) == 1:
            for t in tracks:
                run_one(t)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = [pool.submit(run_one, t) for t in tracks]
                for fut in concurrent.futures.as_completed(futs):
                    fut.result()

        if sync_mode and not args.dry_run:
            sync_key = _sync_source_key(targets)
            sync_state = load_sync_state(data_dir)
            prev_paths = {Path(p).resolve() for p in sync_state.get(sync_key, []) if p}
            other_owned = {
                Path(p).resolve()
                for k, plist in sync_state.items()
                if k != sync_key
                for p in plist
                if p
            }
            base_res = download_dir.resolve()
            pruned = 0
            if args.prune and not cancel_event.is_set():
                if counts["failed"] == 0:
                    for p in sorted(prev_paths - expected_paths - other_owned):
                        if base_res in p.parents and p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                            p.unlink(missing_ok=True)
                            p.with_suffix(".lrc").unlink(missing_ok=True)
                            pruned += 1
                elif not args.jsonl:
                    print(f"[WARN] Skipping prune due to {counts['failed']} failed track(s)", flush=True)

            if counts["failed"] == 0 and not cancel_event.is_set():
                sync_state[sync_key] = sorted(str(p) for p in expected_paths)
            else:
                sync_state[sync_key] = sorted(str(p) for p in (prev_paths | expected_paths))
            save_sync_state(data_dir, jobs_lock, sync_state)

            if args.prune and not args.jsonl:
                print(f"[OK] Sync complete: {counts['downloaded']} fetched, {counts['skipped']} skipped, {counts['failed']} failed, {pruned} pruned")
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        final_status = "cancelled" if cancel_event.is_set() else ("done" if counts["failed"] == 0 else "failed")
        job_record["done"] = counts["downloaded"] + counts["skipped"] + counts["dry_run"]
        job_record["failed"] = counts["failed"]
        job_record["status"] = final_status
        job_record["updated_at"] = int(time.time())
        _upsert_job(data_dir, jobs_lock, job_record)

    return 0 if counts["failed"] == 0 and not cancel_event.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(run_download())
