#!/usr/bin/env python3
"""Shared stdlib utilities and mpv UNIX IPC client for musicctl."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

STATUS_PROPS = [
    "idle-active",
    "pause",
    "media-title",
    "filename",
    "time-pos",
    "duration",
    "volume",
    "mute",
    "speed",
    "playlist-count",
    "playlist-pos",
    "loop-file",
    "loop-playlist",
    "audio-codec-name",
    "audio-params/samplerate",
    "audio-params/format",
    "metadata",
]

SESSION_PROPS = [
    "idle-active",
    "playlist",
    "playlist-pos",
    "time-pos",
    "volume",
    "mute",
    "loop-file",
    "loop-playlist",
    "speed",
]


def format_speed(val: Any) -> str:
    """Return clean speed label (e.g. '1.5x') or empty string when ~1.0x."""
    try:
        spd = float(val)
    except (TypeError, ValueError):
        return ""
    if abs(spd - 1.0) <= 0.001:
        return ""
    return f"{round(spd, 2):g}x"


def format_mmss(secs: Any) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    try:
        total = max(0, int(float(secs)))
    except (TypeError, ValueError):
        total = 0
    mins, rem = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}:{mins:02d}:{rem:02d}"
    return f"{mins:02d}:{rem:02d}"


def extract_video_id(value: str) -> str:
    """Extract 11-char YouTube video ID from URL or return raw value."""
    if VIDEO_ID_RE.match(value):
        return value
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", value)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", value)
    if m:
        return m.group(1)
    return value


def _norm_meta(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_TITLE_NOISE_TAG_RE = re.compile(
    r"\s*(?:\((?:official(?:\s+(?:music|lyric|audio))?\s*(?:video|audio|visualizer|mv|m/?v)?|lyric\s*video|lyrics?|visualizer|audio|m/?v|hd|hq|4k|uhd)\)|\[(?:official[^\]]*|lyric\s*video|lyrics?|visualizer|audio|m/?v|hd|hq|4k|uhd)\])",
    re.IGNORECASE,
)
_TITLE_NOISE_TRAIL_RE = re.compile(
    r"\s*(?:\||-)\s*official(?:\s+(?:music|lyric|audio))?\s*(?:video|audio|visualizer|mv)?\s*$",
    re.IGNORECASE,
)


def normalize_track_title(title: str) -> str:
    """Strip YouTube upload noise like (Official Video) while keeping musical qualifiers."""
    raw = (title or "").strip()
    if not raw:
        return ""
    cleaned = raw
    for _ in range(4):
        prev = cleaned
        cleaned = _TITLE_NOISE_TRAIL_RE.sub("", cleaned)
        cleaned = _TITLE_NOISE_TAG_RE.sub("", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -|")
        if cleaned == prev:
            break
    return cleaned or raw


def extract_metadata_fields(metadata: Any) -> dict[str, Any]:
    """Extract normalized artist, album, year, track_num, and isrc from mpv metadata."""
    if not isinstance(metadata, dict):
        return {"artist": "", "album": "", "year": 0, "track_num": 0, "isrc": ""}
    lowered = {
        str(k).lower(): str(v).strip()
        for k, v in metadata.items()
        if v is not None and str(v).strip()
    }
    artist = extract_primary_artist(metadata)
    album = lowered.get("album", "")
    year = 0
    for yk in ("date", "year", "release_date", "originaldate"):
        m = re.search(r"\b((?:19|20)\d{2})\b", lowered.get(yk, ""))
        if m:
            year = int(m.group(1))
            break
    track_num = 0
    for tk in ("track", "tracknumber"):
        m = re.match(r"^\s*(\d+)", lowered.get(tk, ""))
        if m:
            track_num = int(m.group(1))
            break
    isrc = lowered.get("isrc", "").upper()
    return {
        "artist": artist,
        "album": album,
        "year": year,
        "track_num": track_num,
        "isrc": isrc,
    }


def extract_primary_artist(metadata: Any) -> str:
    """Extract primary artist string from mpv metadata dict."""
    if not isinstance(metadata, dict):
        return ""
    lowered = {str(k).lower(): str(v).strip() for k, v in metadata.items() if v}
    raw_artist = lowered.get("artist", "")
    uploader = re.sub(r"\s+-\s*Topic$", "", lowered.get("uploader", ""), flags=re.IGNORECASE).strip()

    first_artist = ""
    if raw_artist:
        first_artist = re.split(r"[,;]|\s+(?:feat\.?|ft\.?)\s+", raw_artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()

    if uploader and first_artist and _norm_meta(uploader) in _norm_meta(first_artist):
        return uploader
    return first_artist or uploader


def pick_best_lyrics_candidate(
    items: list[dict[str, Any]],
    title: str,
    artist: str = "",
    duration: float = 0.0,
) -> dict[str, Any] | None:
    """Score LRCLIB search results by artist, title, duration, and synced availability."""
    n_title = _norm_meta(normalize_track_title(title))
    n_artist = _norm_meta(artist)
    best_score = -10**9
    best_item: dict[str, Any] | None = None
    best_artist_ok = False
    best_dur_diff: float | None = None

    for it in items:
        if not isinstance(it, dict):
            continue
        has_synced = bool((it.get("syncedLyrics") or "").strip())
        has_plain = bool((it.get("plainLyrics") or "").strip())
        if not (has_synced or has_plain):
            continue

        c_title = _norm_meta(str(it.get("trackName") or ""))
        c_artist = _norm_meta(str(it.get("artistName") or ""))
        score = 0
        artist_ok = False

        if n_artist:
            if c_artist and (n_artist in c_artist or c_artist in n_artist):
                score += 50
                artist_ok = True
            else:
                score -= 40

        if n_title and c_title:
            if n_title == c_title:
                score += 35
            elif n_title in c_title or c_title in n_title:
                score += 15

        dur_diff: float | None = None
        if duration > 0 and it.get("duration") is not None:
            try:
                dur_diff = abs(float(it["duration"]) - float(duration))
                if dur_diff <= 5.0:
                    score += 30
                elif dur_diff <= 15.0:
                    score += 15
                elif dur_diff > 30.0:
                    score -= 30
            except (TypeError, ValueError):
                dur_diff = None

        if has_synced:
            score += 10

        if score > best_score:
            best_score = score
            best_item = it
            best_artist_ok = artist_ok
            best_dur_diff = dur_diff

    if best_item is None:
        return None
    if n_artist and not best_artist_ok:
        if duration <= 0 or best_dur_diff is None or best_dur_diff > 15.0:
            return None
    return best_item


def parse_synced_lyrics_lines(synced_text: str) -> list[tuple[float, str]]:
    """Parse LRC synced lyrics text into sorted (seconds, line_text) tuples."""
    parsed: list[tuple[float, str]] = []
    for raw_line in (synced_text or "").splitlines():
        matches = list(TS_RE.finditer(raw_line))
        if not matches:
            continue
        text = TS_RE.sub("", raw_line).strip() or "..."
        for m in matches:
            mins = int(m.group(1))
            secs = int(m.group(2))
            frac = m.group(3) or "0"
            ms = float("0." + frac)
            parsed.append((round(mins * 60 + secs + ms, 3), text))
    parsed.sort(key=lambda x: x[0])
    return parsed


def parse_fallback_lyrics_json(
    raw_json: str, title: str, artist: str = ""
) -> tuple[str, list[tuple[float | None, str]]] | None:
    """Parse secondary plain-lyrics JSON (e.g. lyrics.ovh) into header and lines."""
    if not raw_json or not raw_json.strip():
        return None
    try:
        obj = json.loads(raw_json)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    body = str(obj.get("lyrics") or "").replace("\r\n", "\n").strip()
    if not body:
        return None
    body = re.sub(r"(?is)^paroles de la chanson[^\n]*\n+", "", body).strip()
    if not body:
        return None
    hdr = f"{title} - {artist}" if artist else title
    return f"{hdr} [Fallback]", [(None, ln) for ln in body.splitlines()]


def parse_sponsorblock_segments(
    raw_json: str, categories: tuple[str, ...] = ("music_offtopic",)
) -> list[dict[str, Any]]:
    """Parse SponsorBlock skipSegments JSON, filtering >=0.5s and merging overlaps."""
    if not raw_json or not raw_json.strip():
        return []
    try:
        data = json.loads(raw_json)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    raw_segs: list[tuple[float, float, str]] = []
    allowed = {c.lower() for c in categories}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category") or "").strip().lower()
        if allowed and cat not in allowed:
            continue
        seg = entry.get("segment")
        if not isinstance(seg, (list, tuple)) or len(seg) < 2:
            continue
        try:
            start = float(seg[0])
            end = float(seg[1])
        except (TypeError, ValueError):
            continue
        if start < 0 or end <= start or (end - start) < 0.5:
            continue
        raw_segs.append((start, end, cat))

    if not raw_segs:
        return []
    raw_segs.sort(key=lambda x: x[0])

    merged: list[dict[str, Any]] = []
    cur_s, cur_e, cur_cat = raw_segs[0]
    for s, e, cat in raw_segs[1:]:
        if s <= cur_e + 0.1:
            if e > cur_e:
                cur_e = e
        else:
            merged.append({"start": round(cur_s, 3), "end": round(cur_e, 3), "category": cur_cat})
            cur_s, cur_e, cur_cat = s, e, cat
    merged.append({"start": round(cur_s, 3), "end": round(cur_e, 3), "category": cur_cat})
    return merged


def fetch_sponsorblock_segments(video_id: str, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Fetch SponsorBlock music_offtopic segments for a YouTube video ID."""
    if not VIDEO_ID_RE.match(video_id or ""):
        return []
    url = (
        "https://sponsor.ajay.app/api/skipSegments?"
        + urllib.parse.urlencode(
            {
                "videoID": video_id,
                "categories": json.dumps(["music_offtopic"]),
            }
        )
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "musicctl/2.2"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return parse_sponsorblock_segments(raw)
    except Exception:
        return []


def should_trigger_autoplay(
    autoplay_on: bool,
    loop_mode: str,
    queue_pos: int,
    queue_count: int,
    video_id: str,
    last_seeded_id: str = "",
) -> bool:
    """Return True when Infinite Autoplay should enqueue related tracks."""
    if not autoplay_on or loop_mode != "off":
        return False
    if queue_count < 1 or queue_pos < queue_count:
        return False
    if not VIDEO_ID_RE.match(video_id or "") or video_id == last_seeded_id:
        return False
    return True


class MpvClient:
    """Fast single-socket UNIX IPC client for mpv."""

    def __init__(self, sock_path: str, timeout: float = 0.4) -> None:
        self.sock_path = sock_path
        self.timeout = timeout

    def raw_request(self, raw_cmd: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Send raw JSON command (list or dict) and return (ok, response_line)."""
        if not os.path.exists(self.sock_path):
            return False, ""
        try:
            cmd_obj = json.loads(raw_cmd)
        except Exception:
            return False, ""
        if isinstance(cmd_obj, list):
            req = {"command": cmd_obj, "request_id": 1}
        elif isinstance(cmd_obj, dict):
            req = dict(cmd_obj)
            req.setdefault("request_id", 1)
        else:
            return False, ""
        req_id = req["request_id"]
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock_path)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    return False, ""
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if "error" in obj and obj.get("request_id") == req_id:
                        return obj.get("error") == "success", text
        except OSError:
            return False, ""
        finally:
            s.close()

    def batch_get(self, props: list[str]) -> dict[str, Any] | None:
        if not props or not os.path.exists(self.sock_path):
            return None
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.sock_path)
            payload = "".join(
                json.dumps({"command": ["get_property", p], "request_id": idx}) + "\n"
                for idx, p in enumerate(props, 1)
            )
            s.sendall(payload.encode("utf-8"))
            results: dict[str, Any] = {}
            seen_ids: set[int] = set()
            buf = b""
            while len(seen_ids) < len(props):
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        obj = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    req_id = obj.get("request_id")
                    if isinstance(req_id, int) and 1 <= req_id <= len(props):
                        seen_ids.add(req_id)
                        if obj.get("error") == "success":
                            results[props[req_id - 1]] = obj.get("data")
                        else:
                            results[props[req_id - 1]] = None
            return results if seen_ids else None
        except OSError:
            return None
        finally:
            s.close()

    def send(self, command: list[Any]) -> bool:
        ok, _ = self.raw_request(json.dumps(command), timeout=self.timeout)
        return ok

    def batch_status_snapshot(self) -> dict[str, Any] | None:
        """Query all playback status properties in one IPC roundtrip."""
        raw = self.batch_get(STATUS_PROPS)
        if not raw:
            return None

        lf = str(raw.get("loop-file") or "")
        lp = str(raw.get("loop-playlist") or "")
        loop_mode = "one" if lf in ("inf", "True", "true") else ("all" if lp in ("inf", "True", "true") else "off")

        try:
            spd = float(raw.get("speed") or 1.0)
        except Exception:
            spd = 1.0
        speed_label = format_speed(spd)

        try:
            srate = int(raw.get("audio-params/samplerate") or 0)
        except Exception:
            srate = 0

        qpos_raw = raw.get("playlist-pos")
        qpos = (int(qpos_raw) + 1) if isinstance(qpos_raw, int) and qpos_raw >= 0 else 1
        qcount_raw = raw.get("playlist-count")
        qcount = int(qcount_raw) if isinstance(qcount_raw, int) and qcount_raw >= 0 else 1
        meta_fields = extract_metadata_fields(raw.get("metadata"))

        return {
            "idle": bool(raw.get("idle-active") if raw.get("idle-active") is not None else True),
            "paused": bool(raw.get("pause") or False),
            "title": str(raw.get("media-title") or raw.get("filename") or "Unknown"),
            "artist": meta_fields["artist"],
            "album": meta_fields["album"],
            "year": meta_fields["year"],
            "track_num": meta_fields["track_num"],
            "isrc": meta_fields["isrc"],
            "pos": float(raw.get("time-pos") or 0.0),
            "duration": float(raw.get("duration") or 0.0),
            "volume": int(round(float(raw.get("volume") or 100.0))),
            "muted": bool(raw.get("mute") or False),
            "speed": round(spd, 4),
            "speed_label": speed_label,
            "queue_pos": qpos,
            "queue_count": qcount,
            "loop": loop_mode,
            "codec": str(raw.get("audio-codec-name") or ""),
            "samplerate": srate,
            "sample_fmt": str(raw.get("audio-params/format") or ""),
        }

    def batch_session_snapshot(self) -> dict[str, Any] | None:
        """Query full playback session state in one IPC roundtrip."""
        raw = self.batch_get(SESSION_PROPS)
        if not raw or raw.get("idle-active") is not False:
            return None

        pl_raw = raw.get("playlist")
        if not isinstance(pl_raw, list):
            return None
        playlist = [
            entry["filename"]
            for entry in pl_raw
            if isinstance(entry, dict) and entry.get("filename")
        ]
        if not playlist:
            return None

        lf = str(raw.get("loop-file") or "")
        lp = str(raw.get("loop-playlist") or "")
        loop_mode = "one" if lf in ("inf", "True", "true") else ("all" if lp in ("inf", "True", "true") else "off")

        try:
            spd_f = float(raw.get("speed") if raw.get("speed") is not None else 1.0)
        except (TypeError, ValueError):
            spd_f = 1.0
        spd: int | float = int(spd_f) if spd_f.is_integer() else spd_f

        try:
            vol_f = float(raw.get("volume") if raw.get("volume") is not None else 100.0)
        except (TypeError, ValueError):
            vol_f = 100.0
        vol: int | float = int(vol_f) if vol_f.is_integer() else vol_f

        muted_raw = raw.get("mute")
        muted = muted_raw is True or str(muted_raw).lower() == "true"

        return {
            "playlist": playlist,
            "playlist_pos": int(raw.get("playlist-pos") or 0),
            "time_pos": float(raw.get("time-pos") or 0.0),
            "volume": vol,
            "muted": muted,
            "loop": loop_mode,
            "speed": spd,
            "saved_at": int(time.time()),
        }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return 1
    cmd = args[0]

    if cmd == "mpv-ipc" and len(args) >= 3:
        ok, text = MpvClient(args[1]).raw_request(args[2], timeout=2.0)
        if text:
            print(text)
        return 0 if ok else 1

    if cmd == "mpv-status" and len(args) >= 2:
        snap = MpvClient(args[1], timeout=0.8).batch_status_snapshot()
        if snap is None:
            return 1
        print(json.dumps(snap))
        return 0

    if cmd == "mpv-session" and len(args) >= 2:
        snap = MpvClient(args[1], timeout=1.0).batch_session_snapshot()
        if snap is None:
            return 1
        print(json.dumps(snap))
        return 0

    if cmd == "primary-artist" and len(args) >= 3:
        print(extract_primary_artist({"artist": args[1], "uploader": args[2]}))
        return 0

    if cmd == "clean-title" and len(args) >= 2:
        print(normalize_track_title(args[1]))
        return 0

    if cmd == "pick-lyrics" and len(args) >= 4:
        try:
            items = json.load(sys.stdin)
        except Exception:
            return 1
        if not isinstance(items, list):
            return 1
        try:
            dur = float(args[3])
        except Exception:
            dur = 0.0
        best = pick_best_lyrics_candidate(items, args[1], args[2], dur)
        print(json.dumps([best]) if best else "[]")
        return 0

    if cmd == "parse-synced-lyrics":
        try:
            data = json.load(sys.stdin)
        except Exception:
            return 1
        if not isinstance(data, list) or not data:
            return 1
        chosen = next((item for item in data if isinstance(item, dict) and item.get("syncedLyrics")), None)
        if not chosen:
            return 1
        lines = parse_synced_lyrics_lines(str(chosen.get("syncedLyrics") or ""))
        if not lines:
            return 1
        print(
            json.dumps(
                {
                    "title": chosen.get("trackName") or "Unknown",
                    "artist": chosen.get("artistName") or "Unknown",
                    "lines": [{"t": t, "text": txt} for t, txt in lines],
                }
            )
        )
        return 0

    if cmd == "parse-fallback-lyrics":
        title = (args[1] if len(args) > 1 else "") or "Unknown"
        artist = args[2] if len(args) > 2 else ""
        res = parse_fallback_lyrics_json(sys.stdin.read(), title, artist)
        if res is None:
            return 1
        hdr, lines = res
        print(f"=== {hdr} ===\n")
        print("\n".join(txt for _, txt in lines))
        return 0

    if cmd == "parse-sponsorblock":
        print(json.dumps(parse_sponsorblock_segments(sys.stdin.read())))
        return 0

    if cmd == "fetch-sponsorblock" and len(args) >= 2:
        print(json.dumps(fetch_sponsorblock_segments(args[1])))
        return 0

    if cmd == "run-hook" and len(args) >= 2:
        event = args[1]
        hdir = args[2] if len(args) > 2 and args[2] else None
        raw_payload = args[3] if len(args) > 3 else sys.stdin.read()
        try:
            payload = json.loads(raw_payload) if (raw_payload or "").strip() else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        trigger_hooks(event, payload, hooks_dir=hdir, timeout=3.0)
        return 0

    return 1


def _discover_hook_scripts(base_dir: Path, event: str) -> list[Path]:
    """Return executable hook scripts for an event (<dir>/<event> and <dir>/<event>.d/*)."""
    if not base_dir.is_dir() or not event or "/" in event or "\\" in event:
        return []
    scripts: list[Path] = []
    direct = base_dir / event
    if (direct.is_file() or direct.is_symlink()) and os.access(direct, os.X_OK):
        scripts.append(direct)
    dropin = base_dir / f"{event}.d"
    if dropin.is_dir():
        try:
            items = sorted(dropin.iterdir(), key=lambda p: p.name.encode("utf-8", errors="replace"))
            for item in items:
                if not item.is_dir() and (item.is_file() or item.is_symlink()) and os.access(item, os.X_OK):
                    scripts.append(item)
        except OSError:
            pass
    return scripts


def trigger_hooks(
    event: str,
    payload: dict[str, Any],
    hooks_dir: Path | str | None = None,
    timeout: float = 3.0,
) -> int:
    """Invoke lifecycle hook executables with strict timeout; never raises or blocks caller."""
    try:
        raw_dir = str(hooks_dir) if hooks_dir else (os.environ.get("MUSICCTL_HOOKS_DIR") or "")
        if not raw_dir:
            raw_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "musicctl", "hooks")
        scripts = _discover_hook_scripts(Path(raw_dir), str(event or "").strip())
        if not scripts:
            return 0
        body_obj = dict(payload or {})
        body_obj["event"] = str(event)
        body_obj["timestamp"] = int(time.time())
        body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        ran = 0
        for script in scripts:
            try:
                subprocess.run(
                    [str(script)],
                    input=body,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=timeout,
                )
            except Exception:
                pass
            ran += 1
        return ran
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
