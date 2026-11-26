#!/usr/bin/env python3
"""Incremental local audio library indexer and artist/album grouper for musicctl."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from common import AUDIO_EXTS, format_mmss

CACHE_VERSION = 1
MAX_SCAN_DEPTH = 4

_BRACKET_ID_RE = re.compile(r"\s+\[[A-Za-z0-9_.-]{4,32}\]$")
_LEAD_TRACK_RE = re.compile(r"^(\d{1,3})\s*[-._]\s*(.+)$")
_YEAR_RE = re.compile(r"((?:19|20)\d{2})")
_INT_LEAD_RE = re.compile(r"^(\d+)")


def format_duration(sec: int) -> str:
    """Format integer seconds into M:SS or H:MM:SS."""
    return format_mmss(sec, pad_minutes=False)


def parse_filename_fallback(path: Path, root: Path) -> dict[str, Any]:
    """Derive fallback track metadata from filename and relative folder structure."""
    stem = _BRACKET_ID_RE.sub("", path.stem).strip() or path.stem
    track_num = 0
    m_trk = _LEAD_TRACK_RE.match(stem)
    if m_trk:
        track_num = int(m_trk.group(1))
        stem = m_trk.group(2).strip()

    artist = ""
    title = stem
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        if left.strip() and right.strip():
            artist = left.strip()
            title = right.strip()

    album = ""
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts[:-1]
    except ValueError:
        rel_parts = ()

    if len(rel_parts) >= 2:
        artist = artist or rel_parts[-2]
        album = rel_parts[-1]
    elif len(rel_parts) == 1:
        artist = artist or rel_parts[0]

    return {
        "title": title or path.stem,
        "artist": artist,
        "album": album,
        "track": track_num,
        "year": 0,
        "duration": 0,
        "codec": path.suffix.lstrip(".").lower(),
    }


def _parse_int_tag(val: Any) -> int:
    if val is None:
        return 0
    m = _INT_LEAD_RE.match(str(val).strip())
    return int(m.group(1)) if m else 0


def _parse_year_tag(val: Any) -> int:
    if val is None:
        return 0
    m = _YEAR_RE.search(str(val))
    return int(m.group(1)) if m else 0


def probe_audio_file(path: Path, root: Path) -> dict[str, Any]:
    """Extract normalized audio tags via ffprobe with filename fallback."""
    meta = parse_filename_fallback(path, root)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return meta
    except OSError:
        return meta

    if not shutil.which("ffprobe"):
        return meta

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name:stream_tags:format=duration:format_tags",
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
    except Exception:
        return meta

    if proc.returncode != 0 or not proc.stdout:
        return meta

    try:
        data = json.loads(proc.stdout)
    except Exception:
        return meta

    streams = data.get("streams") or []
    st = streams[0] if streams else {}
    fmt = data.get("format") or {}

    tags: dict[str, str] = {}
    for src_tags in (st.get("tags"), fmt.get("tags")):
        if isinstance(src_tags, dict):
            for k, v in src_tags.items():
                if v is not None and str(v).strip():
                    tags[str(k).lower()] = str(v).strip()

    codec = str(st.get("codec_name") or "").strip().lower()
    if codec:
        meta["codec"] = codec

    try:
        dur = float(fmt.get("duration") or 0.0)
        if dur > 0:
            meta["duration"] = int(round(dur))
    except (TypeError, ValueError):
        pass

    if tags.get("title"):
        meta["title"] = tags["title"]
    artist_tag = tags.get("artist") or tags.get("album_artist") or ""
    if artist_tag:
        meta["artist"] = artist_tag
    if tags.get("album"):
        meta["album"] = tags["album"]

    trk = _parse_int_tag(tags.get("track") or tags.get("tracknumber"))
    if trk > 0:
        meta["track"] = trk

    yr = _parse_year_tag(tags.get("date") or tags.get("year"))
    if yr > 0:
        meta["year"] = yr

    return meta


def discover_audio_files(download_dir: Path) -> list[Path]:
    """Find audio files inside download_dir up to MAX_SCAN_DEPTH."""
    if not download_dir.is_dir():
        return []
    root_res = download_dir.resolve()
    found: list[Path] = []
    for cur_dir, dirs, files in os.walk(root_res):
        cur_path = Path(cur_dir)
        rel_parts = () if cur_path == root_res else cur_path.relative_to(root_res).parts
        # Prune hidden directories and cap recursion so files do not exceed MAX_SCAN_DEPTH.
        if len(rel_parts) + 1 >= MAX_SCAN_DEPTH:
            dirs[:] = []
        else:
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            p = cur_path / fname
            if p.suffix.lower() in AUDIO_EXTS:
                found.append(p)
    found.sort(key=lambda p: str(p))
    return found


def load_index(cache_path: Path, root: Path) -> dict[str, dict[str, Any]]:
    """Load incremental library index JSON matching CACHE_VERSION and root."""
    if not cache_path.is_file():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("version") != CACHE_VERSION:
        return {}
    if payload.get("root") != str(root.resolve()):
        return {}
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, dict):
        return {}
    return {str(k): dict(v) for k, v in raw_entries.items() if isinstance(v, dict)}


def save_index(cache_path: Path, root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Persist library index JSON atomically."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "root": str(root.resolve()),
        "entries": entries,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=".library.", suffix=".tmp", dir=str(cache_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_name, cache_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def scan_library(
    download_dir: Path | str,
    cache_path: Path | str,
    *,
    rescan: bool = False,
    prober: Callable[[Path, Path], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Incrementally index audio files keyed by (path, mtime_ns, size)."""
    root = Path(download_dir)
    if not root.is_dir():
        return []
    cache_file = Path(cache_path)
    existing = {} if rescan else load_index(cache_file, root)
    probe_fn = prober or probe_audio_file

    updated: dict[str, dict[str, Any]] = {}
    dirty = rescan or not cache_file.is_file()
    files = discover_audio_files(root)

    for p in files:
        key = str(p.resolve())
        try:
            st = p.stat()
        except OSError:
            continue
        cached = existing.get(key)
        if (
            not rescan
            and cached is not None
            and cached.get("mtime_ns") == st.st_mtime_ns
            and cached.get("size") == st.st_size
        ):
            updated[key] = cached
            continue

        meta = dict(probe_fn(p, root))
        entry = {
            "path": key,
            "filename": p.name,
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "title": str(meta.get("title") or p.stem),
            "artist": str(meta.get("artist") or ""),
            "album": str(meta.get("album") or ""),
            "track": int(meta.get("track") or 0),
            "year": int(meta.get("year") or 0),
            "duration": int(meta.get("duration") or 0),
            "codec": str(meta.get("codec") or p.suffix.lstrip(".").lower()),
        }
        updated[key] = entry
        dirty = True

    if set(existing.keys()) - set(updated.keys()):
        dirty = True

    if dirty:
        save_index(cache_file, root, updated)

    return [updated[k] for k in sorted(updated.keys())]


def filter_entries(entries: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Case-insensitive multi-token substring search over indexed track fields."""
    tokens = [t.lower() for t in (query or "").split() if t.strip()]
    if not tokens:
        return list(entries)
    matched: list[dict[str, Any]] = []
    for e in entries:
        haystack = " ".join(
            [
                str(e.get("artist") or ""),
                str(e.get("album") or ""),
                str(e.get("title") or ""),
                str(e.get("filename") or ""),
                str(e.get("codec") or ""),
            ]
        ).lower()
        if all(tok in haystack for tok in tokens):
            matched.append(e)
    return matched


def _track_sort_key(e: dict[str, Any]) -> tuple[Any, ...]:
    trk = int(e.get("track") or 0)
    return ((0, trk) if trk > 0 else (1, 0), str(e.get("title") or "").lower(), str(e.get("path") or ""))


def group_by_artist(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group indexed entries by artist with sorted album/track ordering."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        artist = str(e.get("artist") or "").strip() or "Unknown Artist"
        buckets.setdefault(artist, []).append(e)

    groups: list[dict[str, Any]] = []
    for artist in sorted(buckets.keys(), key=lambda a: (1 if a == "Unknown Artist" else 0, a.lower())):
        tracks = sorted(
            buckets[artist],
            key=lambda e: (str(e.get("album") or "").lower(), *_track_sort_key(e)),
        )
        groups.append({"artist": artist, "tracks": tracks})
    return groups


def group_by_album(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group indexed entries by album, deriving uniform artist or Various Artists."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        album = str(e.get("album") or "").strip() or "Unknown Album"
        buckets.setdefault(album, []).append(e)

    groups: list[dict[str, Any]] = []
    for album in sorted(buckets.keys(), key=lambda a: (1 if a == "Unknown Album" else 0, a.lower())):
        tracks = sorted(buckets[album], key=_track_sort_key)
        artists = {str(t.get("artist") or "").strip() for t in tracks if str(t.get("artist") or "").strip()}
        if len(artists) == 1:
            artist_lbl = next(iter(artists))
        elif len(artists) > 1:
            artist_lbl = "Various Artists"
        else:
            artist_lbl = "Unknown Artist"
        groups.append({"album": album, "artist": artist_lbl, "tracks": tracks})
    return groups


def _format_track_line(e: dict[str, Any], *, include_album: bool = True) -> str:
    trk = int(e.get("track") or 0)
    prefix = f"{trk:02d}. " if trk > 0 else ""
    title = str(e.get("title") or e.get("filename") or "")
    parts = [f"{prefix}{title}"]
    album = str(e.get("album") or "").strip()
    if include_album and album:
        parts.append(album)
    codec = str(e.get("codec") or "").strip()
    dur = int(e.get("duration") or 0)
    tag_bits = [b for b in (codec, format_duration(dur) if dur > 0 else "") if b]
    if tag_bits:
        parts.append(" / ".join(tag_bits))
    return " - ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musicctl local")
    parser.add_argument("--by", choices=("flat", "artist", "album"), default="flat")
    parser.add_argument("--rescan", action="store_true")
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("query", nargs="*")
    args = parser.parse_args(argv)

    download_dir = Path(os.environ.get("MUSICCTL_DOWNLOAD_DIR") or os.path.expanduser("~/Music/musicctl"))
    cache_dir = Path(
        os.environ.get("MUSICCTL_CACHE_DIR")
        or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "musicctl")
    )
    cache_path = cache_dir / "library.json"

    all_entries = scan_library(download_dir, cache_path, rescan=args.rescan)
    if not all_entries:
        if args.as_json:
            print("[]")
        else:
            print(f"[IDLE] No local audio files in {download_dir}")
        return 0

    query_str = " ".join(args.query).strip()
    entries = filter_entries(all_entries, query_str) if query_str else all_entries
    if not entries:
        if args.as_json:
            print("[]")
        else:
            print(f"[IDLE] No local tracks matching '{query_str}'")
        return 0

    if args.by == "artist":
        grouped = group_by_artist(entries)
        if args.as_json:
            print(json.dumps(grouped, ensure_ascii=False, indent=2))
            return 0
        for g in grouped:
            print(f"{g['artist']} ({len(g['tracks'])} tracks)")
            for t in g["tracks"]:
                print(f"  {_format_track_line(t, include_album=True)}")
        return 0

    if args.by == "album":
        grouped = group_by_album(entries)
        if args.as_json:
            print(json.dumps(grouped, ensure_ascii=False, indent=2))
            return 0
        for g in grouped:
            print(f"{g['album']} - {g['artist']} ({len(g['tracks'])} tracks)")
            for t in g["tracks"]:
                print(f"  {_format_track_line(t, include_album=False)}")
        return 0

    if args.as_json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0

    for idx, e in enumerate(entries, start=1):
        artist = str(e.get("artist") or "").strip()
        title = str(e.get("title") or "").strip()
        fname = str(e.get("filename") or "")
        label = f"{artist} - {title}" if artist and title else (title or fname)
        if label != fname and fname not in label:
            label = f"{label} ({fname})"
        codec = str(e.get("codec") or "").strip()
        dur = int(e.get("duration") or 0)
        suffix = ", ".join(b for b in (codec, format_duration(dur) if dur > 0 else "") if b)
        tail = f" [{suffix}]" if suffix else ""
        print(f"{idx:2d}. {label}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
