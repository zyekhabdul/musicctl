#!/usr/bin/env python3
"""Native clickable & realtime curses Sovereign Dashboard for musicctl."""

from __future__ import annotations

import curses
import dataclasses
import json
import locale
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from common import (
    TS_RE,
    VIDEO_ID_RE,
    MpvClient,
    extract_primary_artist,
    extract_video_id,
    format_mmss,
    format_speed,
    parse_fallback_lyrics_json,
    parse_synced_lyrics_lines,
    pick_best_lyrics_candidate,
)

# UI color pair IDs (1..24 reserved; 25+ dynamic for album art)
PAIR_DEFAULT = 0
PAIR_CYAN = 1
PAIR_ICE = 2
PAIR_PURPLE = 3
PAIR_GREEN = 4
PAIR_AMBER = 5
PAIR_RED = 6
PAIR_DIM = 7
PAIR_BOX = 8
PAIR_SEL = 9
PAIR_PILL_ACTIVE = 10
PAIR_PILL_BTN = 11
PAIR_BAR_FILL = 12
PAIR_BAR_EMPTY = 13
PAIR_LYRIC_ACTIVE = 14
PAIR_TITLE = 15
PAIR_INPUT = 16
PAIR_TOAST = 17
PAIR_SPEC_HIGH = 18

ART_PAIR_START = 25
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
WAVE_BARS = "▁▂▃▄▅▆▇█"


def format_audio_badge(codec: str, samplerate: Any, sample_fmt: str = "") -> str:
    """Format codec, bit depth (for lossless), and kHz into compact badge."""
    c = (codec or "").strip().lower()
    if not c:
        return ""
    bits_lbl = ""
    if c in ("flac", "alac", "wav", "pcm_s16le", "pcm_s24le", "pcm_s32le"):
        fmt = (sample_fmt or "").strip().lower()
        if fmt in ("s16", "s16p"):
            bits_lbl = " 16-bit"
        elif fmt in ("s24", "s24p", "s32", "s32p", "flt", "fltp", "float", "floatp"):
            bits_lbl = " 24-bit"
    hz_lbl = ""
    try:
        sr = int(samplerate or 0)
    except (TypeError, ValueError):
        sr = 0
    if sr > 0:
        khz = sr / 1000.0
        hz_lbl = f" {int(khz)}kHz" if sr % 1000 == 0 else f" {khz:.1f}kHz"
    return f"{c.upper()}{bits_lbl}{hz_lbl}"


def calc_seek_target(rel_x: int, bar_width: int, duration: float) -> float:
    """Map relative click X inside progress bar to target timestamp in seconds."""
    if bar_width <= 1 or duration <= 0:
        return 0.0
    clamped = max(0, min(rel_x, bar_width - 1))
    ratio = clamped / float(bar_width - 1)
    return max(0.0, min(float(duration), ratio * float(duration)))


def calc_vol_target(rel_x: int, bar_width: int, max_vol: int = 100) -> int:
    """Map relative click X inside volume bar to target integer volume."""
    if bar_width <= 1:
        return 0
    clamped = max(0, min(rel_x, bar_width - 1))
    ratio = clamped / float(bar_width - 1)
    return int(round(ratio * max_vol))


def apply_search_key(query: str, key: int | str) -> tuple[str, str]:
    """Apply a curses get_wch() key to the search query buffer -> (new_query, action)."""
    if key in (27, "\x1b"):
        return query, "blur"
    if key in (10, 13, "\n", "\r", curses.KEY_ENTER):
        return query, "submit"
    if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\b"):
        return query[:-1], "stay"
    if isinstance(key, str) and len(key) == 1 and key.isprintable() and ord(key) >= 32:
        return query + key, "stay"
    return query, "stay"


def display_width(text: str) -> int:
    """Compute terminal column width accounting for wide Unicode characters."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_width(text: str, max_cols: int, ellipsis: str = "…") -> str:
    """Truncate text to fit within max_cols terminal columns."""
    if max_cols <= 0:
        return ""
    if display_width(text) <= max_cols:
        return text
    ell_w = display_width(ellipsis)
    if max_cols <= ell_w:
        return ellipsis[:max_cols]
    out: list[str] = []
    cur = 0
    limit = max_cols - ell_w
    for ch in text:
        cw = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if cur + cw > limit:
            break
        out.append(ch)
        cur += cw
    return "".join(out) + ellipsis


def enrich_tsv_entry(path: Path, vid: str, title: str, meta: str) -> bool:
    """Replace raw Video ID / Direct URL placeholder row in TSV with real title & meta."""
    if not path.is_file() or not vid or not title or title == vid:
        return False
    clean_title = re.sub(r"[\t\r\n]+", " ", title).strip()
    clean_meta = re.sub(r"[\t\r\n]+", " ", meta).strip()
    if not clean_title:
        return False
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    new_lines: list[str] = []
    changed = False
    for line in raw_lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == vid:
            old_title = parts[1].strip()
            old_meta = parts[2].strip() if len(parts) > 2 else ""
            if old_title == vid or old_meta in ("Video ID", "Direct URL", ""):
                new_lines.append(f"{vid}\t{clean_title}\t{clean_meta}")
                changed = True
                continue
        new_lines.append(line)
    if not changed:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True


def parse_ansi_art(ansi_text: str) -> list[list[tuple[str, int, int]]]:
    """Parse 256-color SGR ANSI art into rows of (char, fg256, bg256)."""
    rows: list[list[tuple[str, int, int]]] = []
    cleaned = re.sub(r"\x1b\[\?[0-9;]*[hl]", "", ansi_text)
    for raw_line in cleaned.splitlines():
        if not raw_line:
            continue
        row: list[tuple[str, int, int]] = []
        fg, bg = -1, -1
        pos = 0
        for m in SGR_RE.finditer(raw_line):
            chunk = raw_line[pos:m.start()]
            for ch in chunk:
                row.append((ch, fg, bg))
            pos = m.end()
            codes_str = m.group(1)
            codes = [int(c) for c in codes_str.split(";") if c] if codes_str else [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    fg, bg = -1, -1
                elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                    fg = codes[i + 2]
                    i += 2
                elif c == 48 and i + 2 < len(codes) and codes[i + 1] == 5:
                    bg = codes[i + 2]
                    i += 2
                elif c == 39:
                    fg = -1
                elif c == 49:
                    bg = -1
                i += 1
        tail = raw_line[pos:]
        for ch in tail:
            row.append((ch, fg, bg))
        if row:
            rows.append(row)
    return rows


@dataclasses.dataclass
class Hitbox:
    x0: int
    y0: int
    x1: int
    y1: int
    action: str
    payload: Any = None

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1


class SovereignDashboard:
    """Interactive clickable & realtime curses dashboard for musicctl."""

    TABS = [
        ("home", "1 Home"),
        ("search", "2 Search"),
        ("queue", "3 Queue"),
        ("fav", "4 Favs"),
        ("hist", "5 Hist"),
        ("local", "6 Local"),
    ]
    RIGHT_TABS = [("upnext", "Up Next"), ("lyrics", "Lyrics"), ("info", "Info")]

    def __init__(self, stdscr: curses.window, initial_query: str = "") -> None:
        self.stdscr = stdscr
        self.bin_path = os.environ.get("MUSICCTL_BIN") or str(Path(__file__).resolve().parent.parent / "bin" / "musicctl")
        self.sock_path = os.environ.get("MUSICCTL_SOCKET") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "musicctl.sock")
        self.data_dir = Path(os.environ.get("MUSICCTL_DATA_DIR") or os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "musicctl"))
        self.cache_dir = Path(os.environ.get("MUSICCTL_CACHE_DIR") or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "musicctl"))
        self.download_dir = Path(os.environ.get("MUSICCTL_DOWNLOAD_DIR") or os.path.expanduser("~/Music/musicctl"))
        self.art_enabled = os.environ.get("MUSICCTL_ART", "1") == "1"

        self.mpv = MpvClient(self.sock_path)
        self.lock = threading.Lock()
        self.ui_events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()

        self.active_tab = "search" if initial_query else "home"
        self.right_tab = "upnext"
        self.search_query = initial_query
        self.search_focus = False
        self.search_loading = False
        self.search_seq = 0
        self.show_help = False

        self.tracks: list[tuple[str, str, str, str]] = []
        self.search_results: list[tuple[str, str, str, str]] = []
        self.selected_idx = 0
        self.scroll_top = 0

        self.mpv_state: dict[str, Any] = {}
        self.eq_mode = os.environ.get("MUSICCTL_EQ", "flat")
        self.norm_on = os.environ.get("MUSICCTL_NORM", "0") == "1"
        self.quality_mode = os.environ.get("MUSICCTL_QUALITY", "cd")
        self.sleep_rem = ""
        self.fav_ids: set[str] = set()

        self.art_cache: dict[str, list[list[tuple[str, int, int]]]] = {}
        self.art_inflight: set[str] = set()
        self.heal_inflight: set[str] = set()
        self.healed_vids: set[str] = set()
        self.art_pairs: dict[tuple[int, int], int] = {}
        self.last_art_vid = ""
        self.next_art_pair = ART_PAIR_START
        self.max_pairs = 64

        self.lyrics_track_key = ""
        self.lyrics_header = ""
        self.lyrics_lines: list[tuple[float | None, str]] = []
        self.lyrics_loading = False

        self.toast_msg = ""
        self.toast_until = 0.0
        self.hitboxes: list[Hitbox] = []
        self.last_active_path = ""
        self.tick = 0

    def init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        self.max_pairs = min(curses.COLOR_PAIRS - 1, 240)
        if curses.COLORS >= 256:
            curses.init_pair(PAIR_CYAN, 117, -1)
            curses.init_pair(PAIR_ICE, 153, -1)
            curses.init_pair(PAIR_PURPLE, 141, -1)
            curses.init_pair(PAIR_GREEN, 120, -1)
            curses.init_pair(PAIR_AMBER, 221, -1)
            curses.init_pair(PAIR_RED, 203, -1)
            curses.init_pair(PAIR_DIM, 244, -1)
            curses.init_pair(PAIR_BOX, 60, -1)
            curses.init_pair(PAIR_SEL, 195, 237)
            curses.init_pair(PAIR_PILL_ACTIVE, 232, 117)
            curses.init_pair(PAIR_PILL_BTN, 252, 236)
            curses.init_pair(PAIR_BAR_FILL, 117, -1)
            curses.init_pair(PAIR_BAR_EMPTY, 239, -1)
            curses.init_pair(PAIR_LYRIC_ACTIVE, 117, -1)
            curses.init_pair(PAIR_TITLE, 255, -1)
            curses.init_pair(PAIR_INPUT, 255, 235)
            curses.init_pair(PAIR_TOAST, 232, 120)
            curses.init_pair(PAIR_SPEC_HIGH, 141, -1)
        else:
            for idx, fg in [
                (PAIR_CYAN, curses.COLOR_CYAN),
                (PAIR_ICE, curses.COLOR_BLUE),
                (PAIR_PURPLE, curses.COLOR_MAGENTA),
                (PAIR_GREEN, curses.COLOR_GREEN),
                (PAIR_AMBER, curses.COLOR_YELLOW),
                (PAIR_RED, curses.COLOR_RED),
                (PAIR_DIM, curses.COLOR_WHITE),
                (PAIR_BOX, curses.COLOR_BLUE),
                (PAIR_SEL, curses.COLOR_BLACK),
                (PAIR_PILL_ACTIVE, curses.COLOR_BLACK),
                (PAIR_PILL_BTN, curses.COLOR_WHITE),
                (PAIR_BAR_FILL, curses.COLOR_CYAN),
                (PAIR_BAR_EMPTY, curses.COLOR_WHITE),
                (PAIR_LYRIC_ACTIVE, curses.COLOR_CYAN),
                (PAIR_TITLE, curses.COLOR_WHITE),
                (PAIR_INPUT, curses.COLOR_WHITE),
                (PAIR_TOAST, curses.COLOR_BLACK),
                (PAIR_SPEC_HIGH, curses.COLOR_MAGENTA),
            ]:
                bg = curses.COLOR_CYAN if idx in (PAIR_PILL_ACTIVE, PAIR_SEL, PAIR_TOAST) else -1
                curses.init_pair(idx, fg, bg)

    def reset_art_pairs_for_vid(self, art_vid: str) -> bool:
        if art_vid and art_vid != self.last_art_vid:
            self.last_art_vid = art_vid
            self.art_pairs.clear()
            self.next_art_pair = ART_PAIR_START
            return True
        return False

    def get_art_pair(self, fg: int, bg: int) -> int:
        if curses.COLORS < 256:
            return PAIR_DEFAULT
        key = (fg, bg)
        if key in self.art_pairs:
            return self.art_pairs[key]
        if self.next_art_pair <= self.max_pairs:
            pid = self.next_art_pair
            self.next_art_pair += 1
            try:
                curses.init_pair(pid, fg, bg)
                self.art_pairs[key] = pid
                return pid
            except curses.error:
                return PAIR_DEFAULT
        return PAIR_DEFAULT

    def set_toast(self, msg: str, duration: float = 3.0) -> None:
        self.toast_msg = msg
        self.toast_until = time.monotonic() + duration

    def read_tsv(self, path: Path, tag: str) -> list[tuple[str, str, str, str]]:
        items: list[tuple[str, str, str, str]] = []
        if not path.is_file():
            return items
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0]:
                items.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "", tag))
        return items

    def refresh_local_files_meta(self) -> None:
        favs = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
        self.fav_ids = {x[0] for x in favs}
        audio_state = self.data_dir / "audio.state"
        if audio_state.is_file():
            parts = audio_state.read_text(encoding="utf-8", errors="replace").strip().split(":")
            if parts and parts[0]:
                self.eq_mode = parts[0]
            if len(parts) > 1:
                self.norm_on = parts[1] == "1"
        qual_state = self.data_dir / "quality.state"
        if qual_state.is_file():
            q_val = qual_state.read_text(encoding="utf-8", errors="replace").strip()
            if q_val in ("small", "cd", "hires", "ask"):
                self.quality_mode = q_val
        sleep_file = self.data_dir / "sleep.state"
        if sleep_file.is_file():
            try:
                pid_s, end_s = sleep_file.read_text(encoding="utf-8").strip().split(":", 1)
                os.kill(int(pid_s), 0)
                rem = int(end_s) - int(time.time())
                self.sleep_rem = f"{rem // 60}m{rem % 60:02d}s" if rem > 0 else ""
            except Exception:
                self.sleep_rem = ""
        else:
            self.sleep_rem = ""

    def load_tab_items(self) -> None:
        self.refresh_local_files_meta()
        if self.active_tab == "home":
            favs = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
            hist = self.read_tsv(self.data_dir / "history.tsv", "[HIS]")
            seen: set[str] = set()
            merged: list[tuple[str, str, str, str]] = []
            for row in favs + hist:
                if row[0] not in seen:
                    seen.add(row[0])
                    merged.append(row)
            self.tracks = merged
        elif self.active_tab == "search":
            self.tracks = list(self.search_results)
        elif self.active_tab == "fav":
            self.tracks = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
        elif self.active_tab == "hist":
            self.tracks = self.read_tsv(self.data_dir / "history.tsv", "[HIS]")
        elif self.active_tab == "queue":
            pl = self.mpv_state.get("playlist") or []
            qitems: list[tuple[str, str, str, str]] = []
            for idx, entry in enumerate(pl):
                fname = entry.get("filename") or ""
                title = entry.get("title") or fname or "Unknown"
                vid = extract_video_id(fname)
                state_lbl = "Active" if entry.get("current") else "Queued"
                qitems.append((vid, title, f"#{idx + 1} • {state_lbl}", "[Q]"))
            self.tracks = qitems
        elif self.active_tab == "local":
            exts = {".opus", ".mp3", ".m4a", ".flac", ".ogg", ".wav"}
            loc: list[tuple[str, str, str, str]] = []
            if self.download_dir.is_dir():
                for p in sorted(self.download_dir.rglob("*")):
                    if p.is_file() and p.suffix.lower() in exts:
                        loc.append((str(p), p.name, "Local Audio", "[LOC]"))
            self.tracks = loc

        if self.selected_idx >= len(self.tracks):
            self.selected_idx = max(0, len(self.tracks) - 1)
        if self.scroll_top > self.selected_idx:
            self.scroll_top = self.selected_idx

    def drain_ui_events(self) -> None:
        while True:
            try:
                kind, payload = self.ui_events.get_nowait()
            except queue.Empty:
                break
            if kind == "cli_done":
                toast_msg, reload_after = payload
                if toast_msg:
                    self.set_toast(toast_msg)
                if reload_after:
                    self.poll_mpv()
                    self.load_tab_items()
            elif kind == "search_done":
                seq, rows, ok = payload
                if seq != self.search_seq:
                    continue
                self.search_loading = False
                if ok:
                    self.search_results = rows
                    if self.active_tab == "search":
                        self.selected_idx = 0
                        self.scroll_top = 0
                        self.load_tab_items()
                    self.set_toast(f"Found {len(rows)} tracks")
                else:
                    self.set_toast("Search failed")
            elif kind == "lyrics_done":
                track_key, header, lines = payload
                if track_key != self.lyrics_track_key:
                    continue
                self.lyrics_header = header
                self.lyrics_lines = lines
                self.lyrics_loading = False
            elif kind == "heal_done":
                vids, any_updated = payload
                for vid in vids:
                    self.heal_inflight.discard(vid)
                if any_updated and self.active_tab in ("home", "hist"):
                    self.load_tab_items()

    def run_cli_async(self, args: list[str], toast: str = "", reload_after: bool = False) -> None:
        def worker() -> None:
            msg = ""
            try:
                proc = subprocess.run(
                    [self.bin_path, *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    msg = toast
                else:
                    err_lines = (proc.stderr or proc.stdout or "Command failed").strip().splitlines()
                    err = err_lines[-1] if err_lines else "Command failed"
                    msg = re.sub(r"\x1b\[[0-9;]*m", "", err)[:42]
            except Exception as exc:
                msg = str(exc)[:42]
            finally:
                self.ui_events.put(("cli_done", (msg, reload_after)))

        threading.Thread(target=worker, daemon=True).start()

    def trigger_search(self, query: str) -> None:
        q = query.strip()
        if not q:
            return
        self.active_tab = "search"
        self.search_loading = True
        self.search_focus = False
        self.search_seq += 1
        seq = self.search_seq
        self.set_toast(f"Searching: {q[:24]}...")

        def worker() -> None:
            rows: list[tuple[str, str, str, str]] = []
            ok = False
            try:
                proc = subprocess.run(
                    [self.bin_path, "search", q],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                )
                for ln in proc.stdout.splitlines():
                    parts = ln.split("\t")
                    if len(parts) >= 2 and parts[0]:
                        rows.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "", "[YTM]"))
                ok = True
            except Exception:
                ok = False
            finally:
                self.ui_events.put(("search_done", (seq, rows, ok)))

        threading.Thread(target=worker, daemon=True).start()

    def ensure_art_async(self, video_id: str) -> None:
        if not self.art_enabled or not VIDEO_ID_RE.match(video_id):
            return
        with self.lock:
            if video_id in self.art_cache or video_id in self.art_inflight:
                return
            self.art_inflight.add(video_id)

        def worker() -> None:
            try:
                art_dir = self.cache_dir / "art"
                art_dir.mkdir(parents=True, exist_ok=True)
                art_file = art_dir / f"{video_id}.jpg"
                if not art_file.is_file() or art_file.stat().st_size == 0:
                    req = urllib.request.Request(
                        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        data = resp.read()
                    if data:
                        tmp = art_dir / f".{video_id}.tmp"
                        tmp.write_bytes(data)
                        tmp.replace(art_file)
                if art_file.is_file() and art_file.stat().st_size > 0:
                    proc = subprocess.run(
                        [
                            "chafa",
                            "--probe=off",
                            "--format=symbols",
                            "--symbols=half",
                            "--colors=256",
                            "--size=22x10",
                            "--animate=off",
                            str(art_file),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=3.0,
                    )
                    parsed = parse_ansi_art(proc.stdout)
                    with self.lock:
                        self.art_cache[video_id] = parsed
            except Exception:
                pass
            finally:
                with self.lock:
                    self.art_inflight.discard(video_id)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_lyrics_async(self, title: str, track_key: str, artist: str = "", duration: float = 0.0) -> None:
        if not title.strip():
            return
        self.lyrics_track_key = track_key
        self.lyrics_loading = True
        lbl = f"{title} - {artist}" if artist else title
        self.lyrics_header = f"Loading lyrics: {lbl[:36]}..."
        self.lyrics_lines = []

        def worker() -> None:
            header = f"No matching lyrics: {lbl}"
            lines: list[tuple[float | None, str]] = []
            try:
                chosen: dict[str, Any] | None = None
                headers = {"User-Agent": "musicctl/2.1"}

                # 1. Exact /api/get match when artist metadata is available
                if artist:
                    params: dict[str, str] = {"track_name": title, "artist_name": artist}
                    if duration > 0:
                        params["duration"] = str(int(round(duration)))
                    get_url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
                    try:
                        req = urllib.request.Request(get_url, headers=headers)
                        with urllib.request.urlopen(req, timeout=5.0) as resp:
                            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
                        if isinstance(obj, dict) and (obj.get("syncedLyrics") or obj.get("plainLyrics")):
                            chosen = obj
                    except Exception:
                        chosen = None

                # 2. Structured /api/search?track_name=&artist_name=
                if chosen is None and artist:
                    s_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"track_name": title, "artist_name": artist})
                    req = urllib.request.Request(s_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        items = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(items, list):
                        chosen = pick_best_lyrics_candidate(items, title, artist, duration)

                # 3. Fallback /api/search?q= with strict artist+duration scoring
                if chosen is None:
                    q_str = f"{artist} {title}".strip() if artist else title
                    q_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": q_str})
                    req = urllib.request.Request(q_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5.0) as resp:
                        items = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(items, list):
                        chosen = pick_best_lyrics_candidate(items, title, artist, duration)

                if not chosen:
                    if artist and title:
                        try:
                            fb_url = (
                                "https://api.lyrics.ovh/v1/"
                                + urllib.parse.quote(artist)
                                + "/"
                                + urllib.parse.quote(title)
                            )
                            req = urllib.request.Request(fb_url, headers=headers)
                            with urllib.request.urlopen(req, timeout=3.0) as resp:
                                fb_raw = resp.read().decode("utf-8", errors="replace")
                            fb_res = parse_fallback_lyrics_json(fb_raw, title, artist)
                            if fb_res is not None:
                                header, lines = fb_res
                        except Exception:
                            pass
                else:
                    t_name = chosen.get("trackName") or title
                    a_name = chosen.get("artistName") or artist
                    header = f"{t_name} - {a_name} [LRCLIB]".strip(" -")
                    synced = (chosen.get("syncedLyrics") or "").strip()
                    if synced:
                        lines = [(t, txt) for t, txt in parse_synced_lyrics_lines(synced)]
                    else:
                        plain = (chosen.get("plainLyrics") or "").strip()
                        lines = [(None, ln) for ln in plain.splitlines()]
            except Exception:
                header = "Lyrics lookup failed"
                lines = []
            finally:
                self.ui_events.put(("lyrics_done", (track_key, header, lines)))

        threading.Thread(target=worker, daemon=True).start()

    def heal_broken_history_async(self) -> None:
        hist_path = self.data_dir / "history.tsv"
        if not hist_path.is_file():
            return
        to_heal: list[str] = []
        for line in hist_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                vid = parts[0].strip()
                title = parts[1].strip()
                meta = parts[2].strip() if len(parts) > 2 else ""
                if VIDEO_ID_RE.match(vid) and (title == vid or meta in ("Video ID", "Direct URL", "")) and vid not in self.heal_inflight:
                    self.heal_inflight.add(vid)
                    to_heal.append(vid)
        if not to_heal:
            return

        def worker() -> None:
            any_updated = False
            try:
                for vid in to_heal:
                    try:
                        oe_url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
                            {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}
                        )
                        req = urllib.request.Request(oe_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=3.0) as resp:
                            data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        t = str(data.get("title") or "").strip()
                        author = re.sub(r"\s+-\s*Topic$", "", str(data.get("author_name") or ""), flags=re.I).strip()
                        if t and enrich_tsv_entry(hist_path, vid, t, author):
                            any_updated = True
                    except Exception:
                        pass
            finally:
                self.ui_events.put(("heal_done", (to_heal, any_updated)))

        threading.Thread(target=worker, daemon=True).start()

    def poll_mpv(self) -> None:
        props = [
            "idle-active",
            "pause",
            "media-title",
            "metadata",
            "filename",
            "path",
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
            "playlist",
        ]
        res = self.mpv.batch_get(props)
        if res is not None:
            self.mpv_state = res
            cur_path = str(res.get("path") or "")
            idle = bool(res.get("idle-active", True))
            vid = extract_video_id(cur_path) if cur_path else ""
            title = str(res.get("media-title") or "")
            artist = extract_primary_artist(res.get("metadata"))
            dur = float(res.get("duration") or 0.0)

            if not idle and VIDEO_ID_RE.match(vid) and title and title != vid and not title.startswith("watch?v=") and vid not in self.healed_vids:
                meta_parts = [p for p in (artist, format_mmss(dur) if dur > 0 else "") if p]
                if enrich_tsv_entry(self.data_dir / "history.tsv", vid, title, " • ".join(meta_parts)):
                    if self.active_tab in ("home", "hist"):
                        self.load_tab_items()
                self.healed_vids.add(vid)

            if cur_path and cur_path != self.last_active_path:
                self.last_active_path = cur_path
                self.ensure_art_async(vid)
                if self.right_tab == "lyrics" and title:
                    self.fetch_lyrics_async(title, cur_path, artist=artist, duration=dur)
                if self.active_tab == "queue":
                    self.load_tab_items()
        else:
            self.mpv_state = {}

    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            x = 0
        avail = w - x
        if avail <= 0:
            return
        clipped = truncate_width(text, avail, "") if display_width(text) > avail else text
        if not clipped:
            return
        try:
            self.stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def add_hitbox(self, x0: int, y0: int, x1: int, y1: int, action: str, payload: Any = None) -> None:
        self.hitboxes.append(Hitbox(x0, y0, x1, y1, action, payload))

    def draw_button(self, y: int, x: int, label: str, action: str, payload: Any = None, active: bool = False, pair: int = PAIR_PILL_BTN) -> int:
        attr = curses.color_pair(PAIR_PILL_ACTIVE if active else pair) | curses.A_BOLD
        btn = f"[{label}]"
        self.safe_addstr(y, x, btn, attr)
        bw = display_width(btn)
        self.add_hitbox(x, y, x + bw, y + 1, action, payload)
        return x + bw + 1

    def get_loop_mode(self) -> str:
        if not self.mpv_state:
            return "off"
        lf = str(self.mpv_state.get("loop-file") or "")
        lp = str(self.mpv_state.get("loop-playlist") or "")
        if lf in ("inf", "True", "true"):
            return "one"
        if lp in ("inf", "True", "true"):
            return "all"
        return "off"

    def render_header(self, w: int) -> None:
        self.safe_addstr(0, 0, " " * w, curses.color_pair(PAIR_PILL_BTN))
        brand = " MUSICCTL v2.1 "
        self.safe_addstr(0, 0, brand, curses.color_pair(PAIR_PILL_ACTIVE) | curses.A_BOLD)
        cx = len(brand) + 1
        for key, lbl in self.TABS:
            is_act = self.active_tab == key
            pill = f" {lbl} "
            pw = len(pill)
            if cx + pw + 18 > w:
                break
            attr = curses.color_pair(PAIR_PILL_ACTIVE if is_act else PAIR_PILL_BTN) | (curses.A_BOLD if is_act else 0)
            self.safe_addstr(0, cx, pill, attr)
            self.add_hitbox(cx, 0, cx + pw, 1, "tab", key)
            cx += pw + 1

        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        paused = bool(self.mpv_state.get("pause", False))
        codec = str(self.mpv_state.get("audio-codec-name") or "")
        srate = self.mpv_state.get("audio-params/samplerate") or 0
        sfmt = str(self.mpv_state.get("audio-params/format") or "")
        badge = "[IDLE]" if idle else ("[PAUSE]" if paused else "[PLAY]")
        badge_pair = PAIR_DIM if idle else (PAIR_AMBER if paused else PAIR_GREEN)
        audio_badge = format_audio_badge(codec, srate, sfmt)
        codec_str = f" {audio_badge}" if audio_badge else ""
        right_str = f"{badge}{codec_str} "
        rx = max(cx, w - len(right_str) - 1)
        self.safe_addstr(0, rx, badge, curses.color_pair(badge_pair) | curses.A_BOLD)
        if codec_str:
            self.safe_addstr(0, rx + len(badge), codec_str, curses.color_pair(PAIR_CYAN))

    def render_search_bar(self, w: int) -> None:
        self.safe_addstr(1, 0, " " * w, curses.color_pair(PAIR_DEFAULT))
        self.safe_addstr(1, 1, "Search:", curses.color_pair(PAIR_ICE) | curses.A_BOLD)
        btn_search_w = 8
        btn_clear_w = 7
        box_x = 9
        box_w = max(12, w - box_x - btn_search_w - btn_clear_w - 5)
        cursor_ch = "_" if self.search_focus else ""
        inner = truncate_width(self.search_query + cursor_ch, max(1, box_w - 2))
        box_txt = " " + inner + " " * max(0, box_w - 1 - display_width(inner))
        box_attr = curses.color_pair(PAIR_INPUT) | (curses.A_BOLD if self.search_focus else 0)
        self.safe_addstr(1, box_x, box_txt, box_attr)
        self.add_hitbox(box_x, 1, box_x + box_w, 2, "focus_search", None)

        bx = box_x + box_w + 1
        search_lbl = "..." if self.search_loading else "Search"
        bx = self.draw_button(1, bx, search_lbl, "do_search", None, active=self.search_loading)
        self.draw_button(1, bx, "Clear", "clear_search", None)

    def render_left_pane(self, y0: int, h: int, x0: int, w: int) -> None:
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", curses.color_pair(PAIR_BOX))
        tab_title = dict(self.TABS).get(self.active_tab, self.active_tab.upper())
        hdr = f" {tab_title} ({len(self.tracks)}) "
        self.safe_addstr(y0, x0 + 2, hdr, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)

        body_h = max(1, h - 2)
        if self.selected_idx < self.scroll_top:
            self.scroll_top = self.selected_idx
        elif self.selected_idx >= self.scroll_top + body_h:
            self.scroll_top = self.selected_idx - body_h + 1

        for row_i in range(body_h):
            ry = y0 + 1 + row_i
            self.safe_addstr(ry, x0, "│", curses.color_pair(PAIR_BOX))
            self.safe_addstr(ry, x0 + w - 1, "│", curses.color_pair(PAIR_BOX))
            idx = self.scroll_top + row_i
            inner_w = max(1, w - 2)
            if idx < len(self.tracks):
                vid, title, meta, tag = self.tracks[idx]
                is_sel = idx == self.selected_idx
                marker = "▸" if is_sel else " "
                fav_mark = "*" if vid in self.fav_ids else " "
                prefix = f"{marker}{idx + 1:2d}.{fav_mark} "
                tag_str = f" {tag}" if tag else ""
                rem_w = max(4, inner_w - len(prefix) - len(tag_str) - 1)
                combined = f"{title} — {meta}" if meta else title
                text = truncate_width(combined, rem_w)
                pad = max(0, inner_w - len(prefix) - display_width(text) - len(tag_str))
                line = f"{prefix}{text}{' ' * pad}{tag_str}"
                attr = curses.color_pair(PAIR_SEL if is_sel else PAIR_DEFAULT) | (curses.A_BOLD if is_sel else 0)
                self.safe_addstr(ry, x0 + 1, line, attr)
                if not is_sel and fav_mark == "*":
                    self.safe_addstr(ry, x0 + 5, "*", curses.color_pair(PAIR_AMBER) | curses.A_BOLD)
                self.add_hitbox(x0 + 1, ry, x0 + w - 1, ry + 1, "row", idx)
            else:
                if row_i == 0 and not self.tracks:
                    msg = " Searching..." if self.search_loading else " Empty. Press / or click Search above."
                    self.safe_addstr(ry, x0 + 1, truncate_width(msg, inner_w), curses.color_pair(PAIR_DIM))
                else:
                    self.safe_addstr(ry, x0 + 1, " " * inner_w, curses.color_pair(PAIR_DEFAULT))

        self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", curses.color_pair(PAIR_BOX))

    def render_right_pane(self, y0: int, h: int, x0: int, w: int) -> None:
        if w < 24 or h < 8:
            return
        top_h = min(13, max(7, h // 2))
        bot_h = max(4, h - top_h)

        # Top Card: NOW PLAYING
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", curses.color_pair(PAIR_BOX))
        self.safe_addstr(y0, x0 + 2, " NOW PLAYING ", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        for r in range(1, top_h - 1):
            self.safe_addstr(y0 + r, x0, "│", curses.color_pair(PAIR_BOX))
            self.safe_addstr(y0 + r, x0 + w - 1, "│", curses.color_pair(PAIR_BOX))
        self.safe_addstr(y0 + top_h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", curses.color_pair(PAIR_BOX))

        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        paused = bool(self.mpv_state.get("pause", False))
        cur_path = str(self.mpv_state.get("path") or "")
        cur_vid = extract_video_id(cur_path) if cur_path else ""
        sel_vid = self.tracks[self.selected_idx][0] if self.tracks and self.selected_idx < len(self.tracks) else ""
        art_vid = cur_vid if VIDEO_ID_RE.match(cur_vid) else sel_vid

        art_rows: list[list[tuple[str, int, int]]] = []
        if self.art_enabled and VIDEO_ID_RE.match(art_vid) and w >= 48 and top_h >= 10:
            self.ensure_art_async(art_vid)
            with self.lock:
                art_rows = self.art_cache.get(art_vid, [])

        info_x = x0 + 2
        if art_rows:
            self.reset_art_pairs_for_vid(art_vid)
            art_w = min(22, max(len(r) for r in art_rows))
            for ri, row_cells in enumerate(art_rows[: top_h - 2]):
                for ci, (ch, fg, bg) in enumerate(row_cells[:art_w]):
                    try:
                        self.stdscr.addstr(y0 + 1 + ri, x0 + 2 + ci, ch, curses.color_pair(self.get_art_pair(fg, bg)))
                    except curses.error:
                        pass
            info_x = x0 + 3 + art_w

        info_w = max(8, x0 + w - 2 - info_x)
        if not idle:
            title = str(self.mpv_state.get("media-title") or self.mpv_state.get("filename") or "Unknown")
            artist = extract_primary_artist(self.mpv_state.get("metadata"))
            codec = str(self.mpv_state.get("audio-codec-name") or "-")
            srate = self.mpv_state.get("audio-params/samplerate") or 0
            sfmt = str(self.mpv_state.get("audio-params/format") or "")
            audio_lbl = format_audio_badge(codec, srate, sfmt) or codec
            qpos = int(self.mpv_state.get("playlist-pos") or 0) + 1
            qcnt = int(self.mpv_state.get("playlist-count") or 1)
            spd_lbl = format_speed(self.mpv_state.get("speed"))
            loop_mode = self.get_loop_mode()

            self.safe_addstr(y0 + 1, info_x, truncate_width(title, info_w), curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
            row_off = 2
            if artist:
                self.safe_addstr(y0 + row_off, info_x, truncate_width(f"by {artist}", info_w), curses.color_pair(PAIR_PURPLE) | curses.A_BOLD)
                row_off += 1
            self.safe_addstr(y0 + row_off, info_x, truncate_width(f"Audio: {audio_lbl}  |  Queue: {qpos}/{qcnt}", info_w), curses.color_pair(PAIR_CYAN))
            row_off += 1
            dsp_line = f"EQ: {self.eq_mode}  |  Norm: {'on' if self.norm_on else 'off'}  |  Loop: {loop_mode}"
            self.safe_addstr(y0 + row_off, info_x, truncate_width(dsp_line, info_w), curses.color_pair(PAIR_ICE))
            row_off += 1
            extra_parts: list[str] = []
            if spd_lbl:
                extra_parts.append(f"Speed: {spd_lbl}")
            if self.sleep_rem:
                extra_parts.append(f"Sleep: {self.sleep_rem}")
            if extra_parts and row_off < top_h - 2:
                self.safe_addstr(y0 + row_off, info_x, truncate_width("  |  ".join(extra_parts), info_w), curses.color_pair(PAIR_AMBER))

            # Animated spectrum bar
            spec_y = y0 + top_h - 2
            if spec_y >= y0 + row_off:
                spec_w = min(info_w, 28)
                bars = []
                for bi in range(spec_w):
                    if paused:
                        bars.append("▁")
                    else:
                        level = int((math.sin(self.tick * 0.7 + bi * 0.55) + 1.0) * 3.5)
                        bars.append(WAVE_BARS[max(0, min(len(WAVE_BARS) - 1, level))])
                self.safe_addstr(spec_y, info_x, "".join(bars), curses.color_pair(PAIR_DIM if paused else PAIR_SPEC_HIGH))
        else:
            self.safe_addstr(y0 + 1, info_x, "No active playback", curses.color_pair(PAIR_DIM) | curses.A_BOLD)
            self.safe_addstr(y0 + 2, info_x, truncate_width("Double-click track or press Enter", info_w), curses.color_pair(PAIR_DIM))
            self.safe_addstr(y0 + 3, info_x, truncate_width(f"EQ: {self.eq_mode}  |  Norm: {'on' if self.norm_on else 'off'}", info_w), curses.color_pair(PAIR_ICE))

        # Bottom Card: Up Next / Lyrics / Info
        by0 = y0 + top_h
        self.safe_addstr(by0, x0, "╭" + "─" * max(0, w - 2) + "╮", curses.color_pair(PAIR_BOX))
        tx = x0 + 2
        for rkey, rlbl in self.RIGHT_TABS:
            if tx + len(rlbl) + 4 >= x0 + w - 1:
                break
            tx = self.draw_button(by0, tx, rlbl, "rtab", rkey, active=(self.right_tab == rkey))

        b_inner_h = max(1, bot_h - 2)
        b_inner_w = max(1, w - 4)
        for r in range(b_inner_h):
            self.safe_addstr(by0 + 1 + r, x0, "│", curses.color_pair(PAIR_BOX))
            self.safe_addstr(by0 + 1 + r, x0 + w - 1, "│", curses.color_pair(PAIR_BOX))

        if self.right_tab == "upnext":
            pl = self.mpv_state.get("playlist") or []
            if not pl:
                self.safe_addstr(by0 + 1, x0 + 2, truncate_width("Queue empty. Right-click a track to enqueue.", b_inner_w), curses.color_pair(PAIR_DIM))
            else:
                cur_i = next((i for i, e in enumerate(pl) if e.get("current")), 0)
                start_i = max(0, cur_i - 1)
                for row_i in range(b_inner_h):
                    qi = start_i + row_i
                    if qi >= len(pl):
                        break
                    entry = pl[qi]
                    is_c = bool(entry.get("current"))
                    qtitle = entry.get("title") or entry.get("filename") or "Unknown"
                    line = truncate_width(f"{'▸' if is_c else ' '} {qi + 1:2d}. {qtitle}", b_inner_w)
                    attr = curses.color_pair(PAIR_GREEN if is_c else PAIR_DEFAULT) | (curses.A_BOLD if is_c else 0)
                    self.safe_addstr(by0 + 1 + row_i, x0 + 2, line, attr)
                    self.add_hitbox(x0 + 2, by0 + 1 + row_i, x0 + w - 1, by0 + 2 + row_i, "jump", qi)
        elif self.right_tab == "lyrics":
            if self.lyrics_loading:
                self.safe_addstr(by0 + 1, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_CYAN))
            elif not self.lyrics_lines:
                self.safe_addstr(by0 + 1, x0 + 2, truncate_width(self.lyrics_header or "Play a track to auto-load lyrics", b_inner_w), curses.color_pair(PAIR_DIM))
            else:
                self.safe_addstr(by0 + 1, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_PURPLE) | curses.A_BOLD)
                tpos = float(self.mpv_state.get("time-pos") or 0.0)
                active_l = 0
                if self.lyrics_lines[0][0] is not None:
                    for li, (ts, _txt) in enumerate(self.lyrics_lines):
                        if ts is not None and ts <= tpos + 0.15:
                            active_l = li
                        else:
                            break
                start_l = max(0, active_l - max(1, (b_inner_h - 1) // 3))
                for row_i in range(max(0, b_inner_h - 1)):
                    li = start_l + row_i
                    if li >= len(self.lyrics_lines):
                        break
                    ts, ltxt = self.lyrics_lines[li]
                    is_cur_l = ts is not None and li == active_l
                    prefix = "▸ " if is_cur_l else "  "
                    attr = curses.color_pair(PAIR_LYRIC_ACTIVE if is_cur_l else (PAIR_DIM if ts is not None and li < active_l else PAIR_DEFAULT))
                    if is_cur_l:
                        attr |= curses.A_BOLD
                    self.safe_addstr(by0 + 2 + row_i, x0 + 2, truncate_width(prefix + ltxt, b_inner_w), attr)
        else:
            if self.tracks and 0 <= self.selected_idx < len(self.tracks):
                svid, stitle, smeta, stag = self.tracks[self.selected_idx]
                self.safe_addstr(by0 + 1, x0 + 2, truncate_width(f"Title: {stitle}", b_inner_w), curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
                if b_inner_h >= 2:
                    self.safe_addstr(by0 + 2, x0 + 2, truncate_width(f"Info : {smeta or '-'}", b_inner_w), curses.color_pair(PAIR_ICE))
                if b_inner_h >= 3:
                    self.safe_addstr(by0 + 3, x0 + 2, truncate_width(f"ID   : {svid} {stag}", b_inner_w), curses.color_pair(PAIR_PURPLE))
                if b_inner_h >= 5:
                    ax = x0 + 2
                    ay = by0 + 4
                    ax = self.draw_button(ay, ax, "Play", "play_sel")
                    ax = self.draw_button(ay, ax, "+Queue", "add_sel")
                    ax = self.draw_button(ay, ax, "~Radio", "radio_sel")
                    if ax + 8 < x0 + w - 1:
                        ax = self.draw_button(ay, ax, "*Fav", "fav_sel")
                    if ax + 11 < x0 + w - 1:
                        ax = self.draw_button(ay, ax, "Download", "dl_sel")
                    if stag == "[Q]" and b_inner_h >= 6:
                        qx = x0 + 2
                        qx = self.draw_button(by0 + 5, qx, "Move Up", "q_up")
                        self.draw_button(by0 + 5, qx, "Move Down", "q_down")
            else:
                self.safe_addstr(by0 + 1, x0 + 2, "No track selected", curses.color_pair(PAIR_DIM))

        self.safe_addstr(by0 + bot_h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", curses.color_pair(PAIR_BOX))

    def render_transport_deck(self, y0: int, w: int) -> None:
        # Row y0: separator
        self.safe_addstr(y0, 0, "─" * w, curses.color_pair(PAIR_BOX))

        # Row y0+1: Clickable Seek Bar
        pos = float(self.mpv_state.get("time-pos") or 0.0)
        dur = float(self.mpv_state.get("duration") or 0.0)
        pos_s = format_mmss(pos)
        dur_s = format_mmss(dur)
        left_lbl = f" {pos_s}  "
        right_lbl = f"  {dur_s} "
        bar_x = len(left_lbl)
        bar_w = max(8, w - bar_x - len(right_lbl) - 1)
        self.safe_addstr(y0 + 1, 0, left_lbl, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        if dur > 0:
            knob = max(0, min(bar_w - 1, int(round((pos / dur) * (bar_w - 1)))))
            filled = "━" * knob + "●"
            empty = "─" * max(0, bar_w - knob - 1)
            self.safe_addstr(y0 + 1, bar_x, filled, curses.color_pair(PAIR_BAR_FILL) | curses.A_BOLD)
            self.safe_addstr(y0 + 1, bar_x + len(filled), empty, curses.color_pair(PAIR_BAR_EMPTY))
        else:
            self.safe_addstr(y0 + 1, bar_x, "─" * bar_w, curses.color_pair(PAIR_BAR_EMPTY))
        self.safe_addstr(y0 + 1, bar_x + bar_w, right_lbl, curses.color_pair(PAIR_DIM))
        self.add_hitbox(bar_x, y0 + 1, bar_x + bar_w, y0 + 2, "seek_bar", (bar_x, bar_w, dur))

        # Row y0+2: Transport Buttons + Clickable Volume Bar
        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        paused = bool(self.mpv_state.get("pause", False))
        bx = 1
        bx = self.draw_button(y0 + 2, bx, "|< Prev", "prev")
        play_lbl = "|> Play" if (idle or paused) else "|| Pause"
        bx = self.draw_button(y0 + 2, bx, play_lbl, "toggle", active=(not idle and not paused))
        bx = self.draw_button(y0 + 2, bx, ">| Next", "next")
        bx = self.draw_button(y0 + 2, bx, "[] Stop", "stop")
        if bx + 28 < w:
            bx = self.draw_button(y0 + 2, bx, "~ Radio", "radio_sel")
            bx = self.draw_button(y0 + 2, bx, "* Fav", "fav_sel")
        if bx + 20 < w:
            bx = self.draw_button(y0 + 2, bx, "+ Queue", "add_sel")

        vol = int(float(self.mpv_state.get("volume") or 100))
        muted = bool(self.mpv_state.get("mute", False))
        vbar_w = 10
        vol_widget_w = 5 + 4 + vbar_w + 6 + 4  # "Vol " + "[-] " + bar + " 100% " + "[+]"
        vx = max(bx + 1, w - vol_widget_w - 1)
        if vx + 22 <= w:
            self.safe_addstr(y0 + 2, vx, "Vol ", curses.color_pair(PAIR_ICE))
            vx += 4
            vx = self.draw_button(y0 + 2, vx, "-", "vol_delta", -5)
            v_fill = max(0, min(vbar_w, int(round((min(vol, 100) / 100.0) * vbar_w))))
            self.safe_addstr(y0 + 2, vx, "█" * v_fill, curses.color_pair(PAIR_RED if muted else PAIR_GREEN))
            self.safe_addstr(y0 + 2, vx + v_fill, "░" * (vbar_w - v_fill), curses.color_pair(PAIR_BAR_EMPTY))
            self.add_hitbox(vx, y0 + 2, vx + vbar_w, y0 + 3, "vol_bar", (vx, vbar_w))
            vx += vbar_w + 1
            self.safe_addstr(y0 + 2, vx, f"{vol:3d}%", curses.color_pair(PAIR_RED if muted else PAIR_DEFAULT))
            vx += 5
            self.draw_button(y0 + 2, vx, "+", "vol_delta", 5)

        # Row y0+3: DSP & Mode Buttons + Toast
        dx = 1
        loop_m = self.get_loop_mode()
        dx = self.draw_button(y0 + 3, dx, f"Loop:{loop_m}", "loop", active=(loop_m != "off"))
        dx = self.draw_button(y0 + 3, dx, "Shuf", "shuffle")
        dx = self.draw_button(y0 + 3, dx, f"Q:{self.quality_mode}", "quality", active=(self.quality_mode != "cd"))
        dx = self.draw_button(y0 + 3, dx, f"EQ:{self.eq_mode}", "eq", active=(self.eq_mode != "flat"))
        dx = self.draw_button(y0 + 3, dx, f"Norm:{'on' if self.norm_on else 'off'}", "norm", active=self.norm_on)
        dx = self.draw_button(y0 + 3, dx, "Mute" if not muted else "UNMUTE", "mute", active=muted, pair=PAIR_RED if muted else PAIR_PILL_BTN)
        dx = self.draw_button(y0 + 3, dx, "? Help", "help", active=self.show_help)

        if self.toast_msg and time.monotonic() < self.toast_until and dx + 6 < w:
            t_str = f" {truncate_width(self.toast_msg, w - dx - 3)} "
            tx = max(dx + 1, w - len(t_str) - 1)
            self.safe_addstr(y0 + 3, tx, t_str, curses.color_pair(PAIR_TOAST) | curses.A_BOLD)

    def render_help_modal(self, h: int, w: int) -> None:
        lines = [
            "MUSICCTL v2.1 - CONTROLS",
            "",
            "Mouse:",
            "  Click track row       Select track",
            "  Double-click row      Play / Jump in Queue",
            "  Right-click row       Enqueue track",
            "  Click progress bar    Seek to timestamp",
            "  Click volume bar      Set volume level",
            "  Click top/right tabs  Switch views & panels",
            "  Mouse wheel           Scroll track list",
            "",
            "Keyboard:",
            "  / or Ctrl-S   Focus search   |  1-6  Switch tabs",
            "  j/k or Arrows Move cursor    |  J/K  Reorder queue track",
            "  Enter Play/Jump  a Enqueue   |  f Fav  r Radio  d Download",
            "  Space Pause/Play  n/b Next/Prev  o Loop  u Shuf  Q Quality",
            "  e Cycle EQ  N Norm  m Mute   |  +/- Vol  [/] Seek",
            "  ? Toggle help  q Quit TUI (daemon stays alive)",
        ]
        mw = min(w - 4, max(len(l) for l in lines) + 4)
        mh = min(h - 2, len(lines) + 2)
        x0 = max(1, (w - mw) // 2)
        y0 = max(1, (h - mh) // 2)
        self.safe_addstr(y0, x0, "╭" + "─" * (mw - 2) + "╮", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        for i in range(1, mh - 1):
            txt = lines[i - 1] if i - 1 < len(lines) else ""
            padded = " " + truncate_width(txt, mw - 4) + " " * max(0, mw - 3 - display_width(truncate_width(txt, mw - 4)))
            self.safe_addstr(y0 + i, x0, "│", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            self.safe_addstr(y0 + i, x0 + 1, padded, curses.color_pair(PAIR_TITLE if i == 1 else PAIR_DEFAULT) | (curses.A_BOLD if i == 1 else 0))
            self.safe_addstr(y0 + i, x0 + mw - 1, "│", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        self.safe_addstr(y0 + mh - 1, x0, "╰" + "─" * (mw - 2) + "╯", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        self.add_hitbox(0, 0, w, h, "close_help", None)

    def render(self) -> None:
        self.stdscr.erase()
        self.hitboxes = []
        h, w = self.stdscr.getmaxyx()
        if h < 12 or w < 44:
            self.safe_addstr(0, 0, f"Terminal too small ({w}x{h}). Resize >= 44x12.", curses.color_pair(PAIR_AMBER))
            self.stdscr.refresh()
            return

        self.render_header(w)
        self.render_search_bar(w)

        deck_h = 4
        mid_y = 2
        mid_h = max(4, h - mid_y - deck_h)
        if w >= 80:
            left_w = max(36, int(w * 0.52))
            right_w = w - left_w
            self.render_left_pane(mid_y, mid_h, 0, left_w)
            self.render_right_pane(mid_y, mid_h, left_w, right_w)
        else:
            self.render_left_pane(mid_y, mid_h, 0, w)

        self.render_transport_deck(h - deck_h, w)
        if self.show_help:
            self.render_help_modal(h, w)
        self.stdscr.refresh()

    def play_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        if tag == "[Q]":
            self.mpv.send(["set_property", "playlist-pos", self.selected_idx])
            self.mpv.send(["set_property", "pause", False])
            self.set_toast(f"Jump #{self.selected_idx + 1}")
        else:
            line = f"{vid}\t{title}\t{meta}\t{tag}"
            self.run_cli_async(["_play_line", line], toast=f"Playing: {title[:24]}", reload_after=True)

    def enqueue_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        line = f"{vid}\t{title}\t{meta}\t{tag}"
        self.run_cli_async(["_add_line", line], toast=f"Queued: {title[:24]}", reload_after=True)

    def handle_action(self, action: str, payload: Any, bstate: int = 0) -> None:
        if action == "close_help":
            self.show_help = False
            return
        if action == "tab":
            self.search_focus = False
            self.active_tab = payload
            self.selected_idx = 0
            self.scroll_top = 0
            self.load_tab_items()
        elif action == "rtab":
            self.right_tab = payload
            if payload == "lyrics":
                title = str(self.mpv_state.get("media-title") or "")
                cur_path = str(self.mpv_state.get("path") or "")
                artist = extract_primary_artist(self.mpv_state.get("metadata"))
                dur = float(self.mpv_state.get("duration") or 0.0)
                if title and cur_path != self.lyrics_track_key:
                    self.fetch_lyrics_async(title, cur_path, artist=artist, duration=dur)
        elif action == "focus_search":
            self.search_focus = True
        elif action == "do_search":
            self.trigger_search(self.search_query)
        elif action == "clear_search":
            self.search_query = ""
            self.search_focus = True
        elif action == "row":
            self.search_focus = False
            self.selected_idx = int(payload)
            if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                self.play_selected()
            elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
                self.enqueue_selected()
        elif action == "jump":
            self.mpv.send(["set_property", "playlist-pos", int(payload)])
            self.mpv.send(["set_property", "pause", False])
            self.set_toast(f"Jump #{int(payload) + 1}")
        elif action == "seek_bar":
            bx, bw, dur = payload
            if dur > 0:
                target = calc_seek_target(self._last_mx - bx, bw, dur)
                self.mpv.send(["seek", target, "absolute"])
                self.set_toast(f"Seek {format_mmss(target)}")
        elif action == "vol_bar":
            vx, vw = payload
            target_v = calc_vol_target(self._last_mx - vx, vw, 100)
            self.mpv.send(["set_property", "volume", target_v])
            self.set_toast(f"Vol {target_v}%")
        elif action == "vol_delta":
            self.run_cli_async(["vol", f"{int(payload):+d}"], toast=f"Vol {int(payload):+d}%")
        elif action == "toggle":
            if self.mpv_state and not self.mpv_state.get("idle-active", True):
                self.mpv.send(["cycle", "pause"])
            else:
                self.play_selected()
        elif action == "prev":
            self.mpv.send(["playlist-prev", "weak"])
        elif action == "next":
            self.mpv.send(["playlist-next", "weak"])
        elif action == "stop":
            self.run_cli_async(["stop"], toast="Daemon stopped", reload_after=True)
        elif action == "loop":
            self.run_cli_async(["loop"], toast="Loop cycled", reload_after=True)
        elif action == "shuffle":
            self.run_cli_async(["shuffle"], toast="Queue shuffled", reload_after=True)
        elif action == "eq":
            self.run_cli_async(["eq", "cycle"], toast="EQ cycled", reload_after=True)
        elif action == "quality":
            self.run_cli_async(["quality", "cycle"], toast="Quality cycled", reload_after=True)
        elif action == "q_up":
            self.move_queue_selected(-1)
        elif action == "q_down":
            self.move_queue_selected(1)
        elif action == "norm":
            self.run_cli_async(["norm", "toggle"], toast="Norm toggled", reload_after=True)
        elif action == "mute":
            self.run_cli_async(["mute"], toast="Mute toggled", reload_after=True)
        elif action == "help":
            self.show_help = not self.show_help
        elif action == "play_sel":
            self.play_selected()
        elif action == "add_sel":
            self.enqueue_selected()
        elif action == "radio_sel":
            if self.tracks and self.selected_idx < len(self.tracks):
                vid = self.tracks[self.selected_idx][0]
                if VIDEO_ID_RE.match(vid):
                    self.run_cli_async(["radio", vid], toast="Queueing YTM Radio...", reload_after=True)
                    return
            self.run_cli_async(["radio", "current"], toast="Queueing YTM Radio...", reload_after=True)
        elif action == "fav_sel":
            if self.tracks and self.selected_idx < len(self.tracks):
                vid, title, meta, _tag = self.tracks[self.selected_idx]
                if vid in self.fav_ids:
                    self.run_cli_async(["fav", "rm", vid], toast="Removed favorite", reload_after=True)
                else:
                    self.run_cli_async(["fav", "add", vid, title, meta], toast="Added favorite", reload_after=True)
        elif action == "dl_sel":
            if self.tracks and self.selected_idx < len(self.tracks):
                vid = self.tracks[self.selected_idx][0]
                self.run_cli_async(["download", vid], toast=f"Downloading ({self.quality_mode})...")

    def move_queue_selected(self, delta: int) -> None:
        if self.active_tab != "queue" or not self.tracks:
            return
        new_idx = self.selected_idx + delta
        if new_idx < 0 or new_idx >= len(self.tracks):
            return
        from_1b = self.selected_idx + 1
        to_1b = new_idx + 1
        self.selected_idx = new_idx
        self.run_cli_async(["move", str(from_1b), str(to_1b)], toast=f"Moved #{from_1b} -> #{to_1b}", reload_after=True)

    def run(self) -> None:
        self.init_colors()
        curses.curs_set(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(180)
        self.stdscr.keypad(True)
        self.stdscr.timeout(250)

        self.poll_mpv()
        self.load_tab_items()
        self.heal_broken_history_async()
        if self.search_query:
            self.trigger_search(self.search_query)

        while True:
            self.drain_ui_events()
            self.tick += 1
            if self.tick % 2 == 0:
                self.poll_mpv()
                if self.active_tab == "queue":
                    self.load_tab_items()
                else:
                    self.refresh_local_files_meta()
            self.render()

            try:
                wch = self.stdscr.get_wch()
            except curses.error:
                continue
            if wch == curses.KEY_RESIZE:
                continue
            if wch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue
                self._last_mx = mx
                if bstate & curses.BUTTON4_PRESSED:
                    self.selected_idx = max(0, self.selected_idx - 3)
                    continue
                if bstate & curses.BUTTON5_PRESSED:
                    self.selected_idx = min(max(0, len(self.tracks) - 1), self.selected_idx + 3)
                    continue
                for hb in reversed(self.hitboxes):
                    if hb.contains(mx, my):
                        self.handle_action(hb.action, hb.payload, bstate)
                        break
                continue

            if self.show_help:
                self.show_help = False
                continue

            if self.search_focus:
                self.search_query, act = apply_search_key(self.search_query, wch)
                if act == "blur":
                    self.search_focus = False
                elif act == "submit":
                    self.trigger_search(self.search_query)
                continue

            ch = ord(wch) if isinstance(wch, str) and len(wch) == 1 else wch

            if ch in (ord("q"), 3):
                break
            elif ch in (ord("/"), 19):  # / or Ctrl-S
                self.search_focus = True
            elif ch == ord("?"):
                self.show_help = True
            elif ch in (ord("j"), curses.KEY_DOWN):
                if self.tracks:
                    self.selected_idx = min(len(self.tracks) - 1, self.selected_idx + 1)
            elif ch in (ord("k"), curses.KEY_UP):
                self.selected_idx = max(0, self.selected_idx - 1)
            elif ch == ord("J") and self.active_tab == "queue":
                self.move_queue_selected(1)
            elif ch == ord("K") and self.active_tab == "queue":
                self.move_queue_selected(-1)
            elif ch == ord("g"):
                self.selected_idx = 0
            elif ch == ord("G"):
                self.selected_idx = max(0, len(self.tracks) - 1)
            elif ch in (10, 13, curses.KEY_ENTER):
                self.play_selected()
            elif ch == ord("a"):
                self.enqueue_selected()
            elif ch == ord("f"):
                self.handle_action("fav_sel", None)
            elif ch == ord("r"):
                self.handle_action("radio_sel", None)
            elif ch == ord("d"):
                self.handle_action("dl_sel", None)
            elif ch == ord(" "):
                self.handle_action("toggle", None)
            elif ch == ord("n"):
                self.handle_action("next", None)
            elif ch == ord("b"):
                self.handle_action("prev", None)
            elif ch == ord("o"):
                self.handle_action("loop", None)
            elif ch == ord("u"):
                self.handle_action("shuffle", None)
            elif ch == ord("e"):
                self.handle_action("eq", None)
            elif ch == ord("Q"):
                self.handle_action("quality", None)
            elif ch == ord("N"):
                self.handle_action("norm", None)
            elif ch == ord("m"):
                self.handle_action("mute", None)
            elif ch == ord("x"):
                self.handle_action("stop", None)
            elif ch in (ord("+"), ord("=")):
                self.handle_action("vol_delta", 5)
            elif ch == ord("-"):
                self.handle_action("vol_delta", -5)
            elif ch == ord("["):
                self.mpv.send(["seek", -10, "relative"])
            elif ch == ord("]"):
                self.mpv.send(["seek", 10, "relative"])
            elif ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6")):
                idx = ch - ord("1")
                self.handle_action("tab", self.TABS[idx][0])
            elif ch == 9:  # Tab cycles right sub-tabs
                cur_r = next((i for i, (k, _) in enumerate(self.RIGHT_TABS) if k == self.right_tab), 0)
                self.handle_action("rtab", self.RIGHT_TABS[(cur_r + 1) % len(self.RIGHT_TABS)][0])


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    initial_query = " ".join(sys.argv[1:]).strip()
    curses.wrapper(lambda stdscr: SovereignDashboard(stdscr, initial_query).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
