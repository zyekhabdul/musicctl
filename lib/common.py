#!/usr/bin/env python3
"""Shared stdlib utilities and mpv UNIX IPC client for musicctl."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
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
    n_title = _norm_meta(title)
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

        return {
            "idle": bool(raw.get("idle-active") if raw.get("idle-active") is not None else True),
            "paused": bool(raw.get("pause") or False),
            "title": str(raw.get("media-title") or raw.get("filename") or "Unknown"),
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

    if cmd == "primary-artist" and len(args) >= 3:
        print(extract_primary_artist({"artist": args[1], "uploader": args[2]}))
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
