#!/usr/bin/env python3
"""Shared stdlib utilities and mpv UNIX IPC client for musicctl."""

from __future__ import annotations

import html
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

USER_AGENT = "musicctl/2.3"
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


def parse_spotify_html(raw_html: str) -> list[tuple[str, str]]:
    """Extract deduplicated (title, artist) pairs from Spotify embed/HTML/oEmbed payloads."""
    raw = (raw_html or "").strip()
    if not raw:
        return []
    rows: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_row(title: Any, artist: Any) -> None:
        t = html.unescape(re.sub(r"[\t\r\n]+", " ", str(title or ""))).strip()
        a = html.unescape(re.sub(r"[\t\r\n]+", " ", str(artist or ""))).strip()
        if not t:
            return
        k = (t.lower(), a.lower())
        if k not in seen:
            seen.add(k)
            rows.append((t, a))

    m_next = re.search(r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", raw, re.S | re.I)
    if m_next:
        try:
            nd = json.loads(m_next.group(1))

            def walk(obj: Any) -> None:
                if isinstance(obj, dict):
                    tl = obj.get("trackList")
                    if isinstance(tl, list):
                        for item in tl:
                            if not isinstance(item, dict):
                                continue
                            t = item.get("title") or item.get("name")
                            a = item.get("subtitle") or ""
                            if not a and isinstance(item.get("artists"), list):
                                a = ", ".join(
                                    str(x.get("name") or "")
                                    for x in item["artists"]
                                    if isinstance(x, dict) and x.get("name")
                                )
                            add_row(t, a)
                    trks = obj.get("tracks")
                    if isinstance(trks, dict) and isinstance(trks.get("items"), list):
                        for it in trks["items"]:
                            if not isinstance(it, dict):
                                continue
                            tr = it.get("track") if isinstance(it.get("track"), dict) else it
                            if isinstance(tr, dict):
                                arts = ", ".join(
                                    str(x.get("name") or "")
                                    for x in (tr.get("artists") or [])
                                    if isinstance(x, dict) and x.get("name")
                                )
                                add_row(tr.get("name"), arts)
                    if obj.get("type") == "track" and obj.get("name"):
                        arts = ", ".join(
                            str(x.get("name") or "")
                            for x in (obj.get("artists") or [])
                            if isinstance(x, dict) and x.get("name")
                        )
                        add_row(obj.get("name"), arts)
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)

            walk(nd)
        except Exception:
            pass
    if rows:
        return rows

    if raw.lstrip().startswith("{"):
        try:
            oe = json.loads(raw)
            if isinstance(oe, dict) and oe.get("title"):
                add_row(oe.get("title"), oe.get("author_name") or "")
                return rows
        except Exception:
            pass

    og_title = ""
    og_desc = ""
    for pat in (
        r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:title[\"']",
    ):
        m = re.search(pat, raw, re.I)
        if m:
            og_title = html.unescape(m.group(1)).strip()
            break
    for pat in (
        r"<meta[^>]+property=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:description[\"']",
    ):
        m = re.search(pat, raw, re.I)
        if m:
            og_desc = html.unescape(m.group(1)).strip()
            break
    if og_title:
        title = re.sub(r"\s*\|\s*Spotify\s*$", "", og_title, flags=re.I).strip()
        title = re.sub(r"\s*[-|]\s*song and lyrics by .*$", "", title, flags=re.I).strip()
        artist = ""
        if og_desc:
            cleaned = re.sub(r"^Listen to .*? on Spotify\.\s*", "", og_desc, flags=re.I)
            m_art = re.match(r"^(.*?)\s*[·•|-]\s*Song\b", cleaned, re.I)
            if m_art:
                artist = m_art.group(1).strip()
            else:
                parts = [p.strip() for p in re.split(r"[·•]", cleaned) if p.strip()]
                if parts and not re.match(r"^(Song|Album|Playlist|\d{4})$", parts[0], re.I):
                    artist = parts[0]
        add_row(title, artist)
    return rows


def fetch_spotify_tracks(
    kind: str,
    sp_id: str,
    url: str = "",
    timeout: float = 8.0,
) -> list[tuple[str, str]]:
    """Fetch (title, artist) pairs from public Spotify embed, page URL, or oEmbed."""
    urls = [f"https://open.spotify.com/embed/{kind}/{sp_id}"]
    if url:
        urls.append(url)
    if kind == "track":
        urls.append(
            "https://open.spotify.com/oembed?"
            + urllib.parse.urlencode({"url": f"https://open.spotify.com/track/{sp_id}"})
        )
    for target_u in urls:
        try:
            req = urllib.request.Request(target_u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = parse_spotify_html(raw)
            if parsed:
                return parsed
        except Exception:
            continue
    return []


def fetch_lyrics(
    title: str,
    artist: str = "",
    duration: float = 0.0,
    query: str = "",
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Fetch lyrics via LRCLIB (/api/get -> /api/search) with lyrics.ovh fallback."""
    t_in = (title or "").strip()
    a_in = (artist or "").strip()
    q_in = (query or "").strip()
    if not t_in and q_in:
        if " - " in q_in and not a_in:
            a_part, t_part = q_in.split(" - ", 1)
            a_in = a_part.strip()
            t_in = t_part.strip()
        else:
            t_in = q_in

    clean_title = normalize_track_title(t_in)
    headers = {"User-Agent": USER_AGENT}
    chosen: dict[str, Any] | None = None

    if a_in and clean_title:
        params: dict[str, str] = {"track_name": clean_title, "artist_name": a_in}
        if duration > 0:
            params["duration"] = str(int(round(duration)))
        try:
            req = urllib.request.Request(
                "https://lrclib.net/api/get?" + urllib.parse.urlencode(params),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(obj, dict) and (obj.get("syncedLyrics") or obj.get("plainLyrics")):
                chosen = obj
        except Exception:
            chosen = None

    if chosen is None and a_in and clean_title:
        try:
            req = urllib.request.Request(
                "https://lrclib.net/api/search?"
                + urllib.parse.urlencode({"track_name": clean_title, "artist_name": a_in}),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                items = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(items, list):
                chosen = pick_best_lyrics_candidate(items, clean_title, a_in, duration)
        except Exception:
            chosen = None

    if chosen is None:
        q_str = q_in or (f"{a_in} {clean_title}".strip() if a_in else clean_title)
        if q_str:
            try:
                req = urllib.request.Request(
                    "https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": q_str}),
                    headers=headers,
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    items = json.loads(resp.read().decode("utf-8", errors="replace"))
                if isinstance(items, list):
                    chosen = pick_best_lyrics_candidate(items, clean_title or q_str, a_in, duration)
            except Exception:
                chosen = None

    if chosen:
        synced = str(chosen.get("syncedLyrics") or "").strip()
        plain = str(chosen.get("plainLyrics") or "").strip()
        if not plain and synced:
            plain = re.sub(r"(?m)^\[[0-9]{1,2}:[0-9]{2}(?:\.[0-9]+)?\]\s*", "", synced).strip()
        if plain or synced:
            return {
                "source": "lrclib",
                "title": str(chosen.get("trackName") or clean_title or "Unknown"),
                "artist": str(chosen.get("artistName") or a_in or "Unknown"),
                "synced_text": synced,
                "plain_text": plain,
                "synced_lines": parse_synced_lyrics_lines(synced) if synced else [],
            }

    if a_in and clean_title:
        try:
            fb_url = (
                "https://api.lyrics.ovh/v1/"
                + urllib.parse.quote(a_in)
                + "/"
                + urllib.parse.quote(clean_title)
            )
            req = urllib.request.Request(fb_url, headers=headers)
            with urllib.request.urlopen(req, timeout=min(3.0, timeout)) as resp:
                fb_raw = resp.read().decode("utf-8", errors="replace")
            fb_res = parse_fallback_lyrics_json(fb_raw, clean_title, a_in)
            if fb_res is not None:
                return {
                    "source": "fallback",
                    "title": clean_title,
                    "artist": a_in,
                    "synced_text": "",
                    "plain_text": "\n".join(txt for _, txt in fb_res[1]),
                    "synced_lines": [],
                }
        except Exception:
            pass
    return None


def format_lyrics_plain(res: dict[str, Any]) -> str:
    """Format lyrics result dict as terminal plain-text output."""
    t = str(res.get("title") or "Unknown")
    a = str(res.get("artist") or "").strip()
    hdr = f"{t} - {a}" if a else t
    if res.get("source") == "fallback":
        hdr = f"{hdr} [Fallback]"
    return f"=== {hdr} ===\n\n{res.get('plain_text') or ''}"


def format_mpv_header_fields(headers: Any) -> str:
    """Format HTTP headers dict into escaped mpv http-header-fields string."""
    if isinstance(headers, str):
        return headers
    if not isinstance(headers, dict) or not headers:
        return ""
    parts: list[str] = []
    for k, v in headers.items():
        if not k:
            continue
        item = f"{k}: {v}".replace("\\", "\\\\").replace(",", "\\,")
        parts.append(item)
    return ",".join(parts)


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
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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

    def batch_send(self, commands: list[list[Any]], timeout: float = 5.0) -> tuple[int, int]:
        """Execute multiple mpv IPC commands over a single socket roundtrip."""
        if not commands:
            return 0, 0
        if not os.path.exists(self.sock_path):
            return 0, len(commands)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.sock_path)
            payload = "".join(
                json.dumps({"command": cmd, "request_id": idx}) + "\n"
                for idx, cmd in enumerate(commands, 1)
            )
            s.sendall(payload.encode("utf-8"))
            ok_count = 0
            seen_ids: set[int] = set()
            buf = b""
            while len(seen_ids) < len(commands):
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
                    if isinstance(req_id, int) and 1 <= req_id <= len(commands) and req_id not in seen_ids:
                        seen_ids.add(req_id)
                        if obj.get("error") == "success":
                            ok_count += 1
            return ok_count, len(commands)
        except OSError:
            return 0, len(commands)
        finally:
            s.close()

    def batch_loadfile(
        self,
        items: list[dict[str, Any]],
        action: str = "play",
        timeout: float = 5.0,
    ) -> tuple[int, int]:
        """Load one or more tracks into mpv in a single pipelined IPC batch."""
        cmds: list[list[Any]] = []
        for idx, item in enumerate(items):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            default_mode = "replace" if (action == "play" and idx == 0) else ("append" if action == "append" else "append-play")
            mode = str(item.get("mode") or default_mode)
            opts: dict[str, str] = {}
            title = str(item.get("title") or "").strip()
            if title and title != url:
                opts["force-media-title"] = title
            hdr_str = format_mpv_header_fields(item.get("headers"))
            if hdr_str:
                opts["http-header-fields"] = hdr_str
            if opts:
                cmds.append(["loadfile", url, mode, -1, opts])
            else:
                cmds.append(["loadfile", url, mode])
        if not cmds:
            return 0, 0
        if action == "play":
            cmds.append(["set_property", "pause", False])
        return self.batch_send(cmds, timeout=timeout)

    def resume_session(self, session_data: dict[str, Any], timeout: float = 5.0) -> int:
        """Restore saved mpv playlist, position, volume, mute, loop, and speed in one batch."""
        playlist = [str(x) for x in (session_data.get("playlist") or []) if x]
        if not playlist:
            return 0
        cmds: list[list[Any]] = [["loadfile", playlist[0], "replace"]]
        for u in playlist[1:]:
            cmds.append(["loadfile", u, "append"])
        cmds.append(["set_property", "playlist-pos", int(session_data.get("playlist_pos") or 0)])
        cmds.append(["set_property", "volume", session_data.get("volume", 100)])
        cmds.append(["set_property", "mute", bool(session_data.get("muted", False))])
        cmds.append(["set_property", "speed", session_data.get("speed", 1)])
        loop_mode = str(session_data.get("loop") or "off")
        cmds.append(["set_property", "loop-file", "inf" if loop_mode == "one" else "no"])
        cmds.append(["set_property", "loop-playlist", "inf" if loop_mode == "all" else "no"])
        cmds.append(["set_property", "pause", False])
        ok_cnt, _ = self.batch_send(cmds, timeout=timeout)
        if ok_cnt <= 0:
            return 0
        try:
            tpos = float(session_data.get("time_pos") or 0.0)
        except (TypeError, ValueError):
            tpos = 0.0
        if tpos > 1.0:
            time.sleep(0.25)
            self.send(["seek", tpos, "absolute"])
        return len(playlist)

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

    if cmd == "parse-spotify-html":
        for t, a in parse_spotify_html(sys.stdin.read()):
            print(f"{t}\t{a}")
        return 0

    if cmd == "fetch-spotify-tracks" and len(args) >= 3:
        rows = fetch_spotify_tracks(args[1], args[2], args[3] if len(args) > 3 else "")
        for t, a in rows:
            print(f"{t}\t{a}")
        return 0

    if cmd == "fetch-lyrics":
        as_json = "--json" in args[1:]
        pos = [a for a in args[1:] if a != "--json"]
        title = pos[0] if len(pos) > 0 else ""
        artist = pos[1] if len(pos) > 1 else ""
        try:
            dur = float(pos[2]) if len(pos) > 2 and pos[2] else 0.0
        except (TypeError, ValueError):
            dur = 0.0
        query = pos[3] if len(pos) > 3 else ""
        res = fetch_lyrics(title, artist=artist, duration=dur, query=query)
        if res is None:
            return 1
        if as_json:
            synced_payload = (
                {
                    "title": res["title"],
                    "artist": res["artist"],
                    "lines": [{"t": t, "text": txt} for t, txt in res["synced_lines"]],
                }
                if res.get("synced_lines")
                else None
            )
            print(
                json.dumps(
                    {
                        "source": res["source"],
                        "title": res["title"],
                        "artist": res["artist"],
                        "plain": format_lyrics_plain(res),
                        "synced_json": synced_payload,
                    }
                )
            )
        else:
            print(format_lyrics_plain(res))
        return 0

    if cmd == "mpv-batch" and len(args) >= 2:
        try:
            cmds = json.load(sys.stdin)
        except Exception:
            return 1
        if not isinstance(cmds, list):
            return 1
        ok_cnt, total = MpvClient(args[1], timeout=5.0).batch_send(cmds, timeout=5.0)
        print(json.dumps({"ok": ok_cnt, "total": total}))
        return 0 if (total == 0 or ok_cnt == total) else 1

    if cmd == "mpv-load-batch" and len(args) >= 2:
        action = args[2] if len(args) > 2 else "play"
        raw = sys.stdin.read().strip()
        if not raw:
            return 1
        items: list[dict[str, Any]] = []
        if raw.startswith("["):
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    items = [x for x in loaded if isinstance(x, dict)]
            except Exception:
                return 1
        else:
            for ln in raw.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                    if isinstance(obj, dict):
                        items.append(obj)
                except Exception:
                    return 1
        ok_cnt, total = MpvClient(args[1], timeout=5.0).batch_loadfile(items, action=action, timeout=5.0)
        print(json.dumps({"ok": ok_cnt, "total": total}))
        return 0 if ok_cnt > 0 else 1

    if cmd == "mpv-resume" and len(args) >= 3:
        try:
            sess = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        except Exception:
            return 1
        if not isinstance(sess, dict):
            return 1
        cnt = MpvClient(args[1], timeout=5.0).resume_session(sess, timeout=5.0)
        if cnt <= 0:
            return 1
        print(cnt)
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
