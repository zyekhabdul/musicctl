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

import catalog
import library

from common import (
    TS_RE,
    VIDEO_ID_RE,
    MpvClient,
    extract_metadata_fields,
    extract_primary_artist,
    extract_video_id,
    fetch_lyrics,
    format_mmss,
    format_speed,
    normalize_track_title,
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
LAYOUT_MODES = ("auto", "split", "stacked", "focus")


def compute_layout(h: int, w: int, mode: str = "auto", show_drawer: bool = True) -> dict[str, Any]:
    """Compute adaptive pane rectangles (y0, h, x0, w) for split, stacked, or focus modes."""
    mid_y = 2
    deck_h = 4
    mid_h = max(4, h - mid_y - deck_h)
    req_mode = mode if mode in LAYOUT_MODES else "auto"

    if req_mode == "focus" or not show_drawer:
        eff = "focus"
        if mid_h >= 14:
            np_rect = (mid_y, 5, 0, w)
            list_rect = (mid_y + 5, mid_h - 5, 0, w)
        else:
            np_rect = None
            list_rect = (mid_y, mid_h, 0, w)
        return {"effective": eff, "list": list_rect, "now_playing": np_rect, "drawer": None}

    use_split = req_mode == "split" or (req_mode == "auto" and w >= 110 and w >= int(h * 2.1))
    if use_split and w >= 64:
        eff = "split"
        left_w = max(36, min(w - 28, int(w * 0.58)))
        right_w = w - left_w
        if mid_h < 8:
            return {
                "effective": eff,
                "list": (mid_y, mid_h, 0, left_w),
                "now_playing": (mid_y, mid_h, left_w, right_w),
                "drawer": None,
            }
        min_top = 8 if mid_h >= 12 else 4
        top_h = min(15, mid_h - 4, max(min_top, int(mid_h * 0.36)))
        bot_h = mid_h - top_h
        return {
            "effective": eff,
            "list": (mid_y, mid_h, 0, left_w),
            "now_playing": (mid_y, top_h, left_w, right_w),
            "drawer": (mid_y + top_h, bot_h, left_w, right_w),
        }

    eff = "stacked"
    if mid_h < 9:
        return {
            "effective": eff,
            "list": (mid_y, mid_h, 0, w),
            "now_playing": None,
            "drawer": None,
        }
    np_h = 9 if mid_h >= 28 else (7 if mid_h >= 18 else 5)
    dr_h = max(6, min(14, int(mid_h * 0.28))) if mid_h >= 16 else 0
    list_h = mid_h - np_h - dr_h
    return {
        "effective": eff,
        "list": (mid_y + np_h, list_h, 0, w),
        "now_playing": (mid_y, np_h, 0, w),
        "drawer": (mid_y + np_h + list_h, dr_h, 0, w) if dr_h > 0 else None,
    }


def build_lightweight_wave(width: int, tick: int, paused: bool) -> str:
    """Build lightweight stdlib full-width audio pulse bar matching card width."""
    if width <= 0:
        return ""
    if paused:
        return "▁" * width
    out: list[str] = []
    n_bars = len(WAVE_BARS) - 1
    for i in range(width):
        v = 0.55 * math.sin(tick * 0.45 + i * 0.35) + 0.45 * math.cos(tick * 0.25 - i * 0.22)
        level = int(round(((v + 1.0) * 0.5) * n_bars))
        out.append(WAVE_BARS[max(0, min(n_bars, level))])
    return "".join(out)


def calc_art_size(card_h: int, card_w: int) -> tuple[int, int] | None:
    """Scale chafa symbol art dimensions to fit NOW PLAYING card safely."""
    if card_h < 6 or card_w < 44:
        return None
    art_h = max(4, min(11, card_h - 2))
    art_w = min(26, max(10, art_h * 2 + 2), max(10, card_w // 3))
    return art_w, art_h


def is_placeholder_title(title: str, vid: str = "") -> bool:
    """Return True when mpv media-title is still an unresolved URL or video ID placeholder."""
    t = (title or "").strip()
    if not t:
        return True
    if vid and t == vid:
        return True
    if t.startswith(("watch?v=", "http://", "https://")):
        return True
    return bool(VIDEO_ID_RE.match(t))


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


def format_album_line(album: str, year: int = 0) -> str:
    """Format album and optional release year for TUI display."""
    alb = (album or "").strip()
    if not alb:
        return ""
    return f"{alb} ({year})" if year > 0 else alb


def extract_album_from_meta(meta: str) -> str:
    """Extract album name from bullet-delimited track metadata string."""
    parts = [p.strip() for p in (meta or "").split("•") if p.strip()]
    if len(parts) < 2 or parts[0] in ("Album", "Single", "EP", "Playlist", "Artist"):
        return ""
    cand = parts[1]
    if re.match(r"^\d+:\d{2}(?::\d{2})?$", cand) or re.match(r"^(?:19|20)\d{2}$", cand):
        return ""
    if cand in ("Video ID", "Direct URL", "Local File", "Local Audio", "Now Playing", "Active", "Queued"):
        return ""
    return cand


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
    if key in (21, 11, "\x15", "\x0b"):
        return "", "stay"
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


def format_track_row_columns(title: str, meta: str, avail_w: int) -> str:
    """Format track title and metadata into aligned columns on wide panes, or compact single line when narrow."""
    if avail_w <= 0:
        return ""
    if not meta or avail_w < 54:
        combined = f"{title} — {meta}" if meta else title
        return truncate_width(combined, avail_w)
    sep = " │ "
    sep_w = display_width(sep)
    title_w = max(22, int((avail_w - sep_w) * 0.56))
    meta_w = avail_w - sep_w - title_w
    if meta_w < 12:
        return truncate_width(f"{title} — {meta}", avail_w)
    t_col = truncate_width(title, title_w)
    t_pad = max(0, title_w - display_width(t_col))
    m_col = truncate_width(meta, meta_w)
    return f"{t_col}{' ' * t_pad}{sep}{m_col}"


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


def parse_download_jsonl_line(line: str) -> dict[str, Any] | None:
    """Parse one --jsonl progress line from lib/downloader.py."""
    s = (line or "").strip()
    if not s or not s.startswith("{"):
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict) or "job_id" not in obj:
        return None
    try:
        pct = max(0, min(100, int(obj.get("pct") or 0)))
    except (TypeError, ValueError):
        pct = 0
    return {
        "job_id": str(obj.get("job_id") or ""),
        "title": str(obj.get("title") or "download"),
        "stage": str(obj.get("stage") or ""),
        "pct": pct,
        "status": str(obj.get("status") or "running"),
        "dest": str(obj.get("dest") or ""),
        "error": str(obj.get("error") or ""),
    }


class DownloadManager:
    """Non-blocking background downloader streaming JSONL progress into ui_events."""

    def __init__(self, bin_path: str, ui_events: queue.SimpleQueue[tuple[str, Any]]) -> None:
        self.bin_path = bin_path
        self.ui_events = ui_events
        self.lock = threading.Lock()
        self.procs: list[subprocess.Popen[str]] = []
        self._seq = 0

    def start(self, dl_args: list[str], label: str = "") -> str:
        with self.lock:
            self._seq += 1
            local_id = f"dlm-{int(time.time())}-{self._seq}"
        disp = (label or " ".join(dl_args) or "download").strip()
        self.ui_events.put(
            (
                "dl_event",
                {
                    "job_id": local_id,
                    "title": disp,
                    "stage": "queued",
                    "pct": 0,
                    "status": "running",
                    "dest": "",
                    "error": "",
                },
            )
        )

        def worker() -> None:
            proc: subprocess.Popen[str] | None = None
            saw_event = False
            try:
                proc = subprocess.Popen(
                    [self.bin_path, "dl", "--jsonl", *dl_args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                with self.lock:
                    self.procs.append(proc)
                if proc.stdout is not None:
                    for raw_line in proc.stdout:
                        ev = parse_download_jsonl_line(raw_line)
                        if ev is not None:
                            saw_event = True
                            ev["_client_id"] = local_id
                            self.ui_events.put(("dl_event", ev))
                _, err_out = proc.communicate()
                if proc.returncode == 0 and not saw_event:
                    self.ui_events.put(
                        (
                            "dl_event",
                            {
                                "job_id": local_id,
                                "title": disp,
                                "stage": "done",
                                "pct": 100,
                                "status": "done",
                                "dest": "",
                                "error": "",
                            },
                        )
                    )
                elif proc.returncode != 0 and not saw_event:
                    err_lines = (err_out or "Download failed").strip().splitlines()
                    err = re.sub(r"\x1b\[[0-9;]*m", "", err_lines[-1] if err_lines else "Download failed")
                    self.ui_events.put(
                        (
                            "dl_event",
                            {
                                "job_id": local_id,
                                "title": disp,
                                "stage": "failed",
                                "pct": 100,
                                "status": "error",
                                "dest": "",
                                "error": err[:80],
                            },
                        )
                    )
            except Exception as exc:
                self.ui_events.put(
                    (
                        "dl_event",
                        {
                            "job_id": local_id,
                            "title": disp,
                            "stage": "failed",
                            "pct": 100,
                            "status": "error",
                            "dest": "",
                            "error": str(exc)[:80],
                        },
                    )
                )
            finally:
                if proc is not None:
                    with self.lock:
                        if proc in self.procs:
                            self.procs.remove(proc)

        threading.Thread(target=worker, daemon=True).start()
        return local_id

    def cancel_all(self) -> int:
        count = 0
        with self.lock:
            active = list(self.procs)
        for p in active:
            if p.poll() is None:
                try:
                    p.terminate()
                    count += 1
                except OSError:
                    pass
        try:
            subprocess.run(
                [self.bin_path, "dl", "cancel", "all"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
            )
        except Exception:
            pass
        return count


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
        ("playlists", "7 Pl"),
    ]
    RIGHT_TABS = [("upnext", "U Up Next"), ("lyrics", "Y Lyrics"), ("info", "I Info"), ("downloads", "D DL")]
    CATALOG_SOURCES = ("ytm", "deezer", "itunes")
    SEARCH_TYPES = ("songs", "albums", "artists", "playlists")
    LOCAL_GROUP_MODES = ("flat", "artist", "album")

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
        self.layout_mode = "auto"
        self.show_drawer = True
        self.focus_pane = "list"
        self.drawer_scroll = 0
        self.drawer_sel = 0
        self.last_list_body_h = 12
        self.last_drawer_body_h = 8
        self.last_layout: dict[str, Any] = {}
        self.search_query = initial_query
        self.search_focus = False
        self.search_loading = False
        self.search_seq = 0
        self.browse_seq = 0
        self.show_help = False

        cat_env = (os.environ.get("MUSICCTL_CATALOG") or "ytm").strip().lower()
        self.catalog_source = cat_env if cat_env in self.CATALOG_SOURCES else "ytm"
        self.search_type = "songs"
        self.local_group_mode = "flat"
        self.nav_stack: list[dict[str, Any]] = []
        self.drill_title = ""
        self.drill_ref = ""
        self.drill_kind = ""
        self._local_groups: dict[str, list[dict[str, Any]]] = {}

        self.tracks: list[tuple[str, str, str, str]] = []
        self.search_results: list[tuple[str, str, str, str]] = []
        self.selected_idx = 0
        self.scroll_top = 0

        self.dl_manager = DownloadManager(self.bin_path, self.ui_events)
        self.dl_jobs: list[dict[str, Any]] = []

        self.mpv_state: dict[str, Any] = {}
        self.eq_mode = os.environ.get("MUSICCTL_EQ", "flat")
        self.norm_on = os.environ.get("MUSICCTL_NORM", "0") == "1"
        self.quality_mode = os.environ.get("MUSICCTL_QUALITY", "cd")
        self.autoplay_on = os.environ.get("MUSICCTL_AUTOPLAY", "0") == "1"
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
        self._file_sigs: dict[str, tuple[int, int] | None] = {}

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

    @staticmethod
    def _file_sig(path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def refresh_local_files_meta(self) -> None:
        sigs = getattr(self, "_file_sigs", None)
        if sigs is None:
            sigs = {}
            self._file_sigs = sigs

        fav_path = self.data_dir / "favorites.tsv"
        fav_sig = self._file_sig(fav_path)
        if fav_sig != sigs.get("fav", ...):
            sigs["fav"] = fav_sig
            favs = self.read_tsv(fav_path, "[FAV]")
            self.fav_ids = {x[0] for x in favs}

        audio_state = self.data_dir / "audio.state"
        audio_sig = self._file_sig(audio_state)
        if audio_sig != sigs.get("audio", ...):
            sigs["audio"] = audio_sig
            if audio_sig is not None:
                parts = audio_state.read_text(encoding="utf-8", errors="replace").strip().split(":")
                if parts and parts[0]:
                    self.eq_mode = parts[0]
                if len(parts) > 1:
                    self.norm_on = parts[1] == "1"

        qual_state = self.data_dir / "quality.state"
        qual_sig = self._file_sig(qual_state)
        if qual_sig != sigs.get("qual", ...):
            sigs["qual"] = qual_sig
            if qual_sig is not None:
                q_val = qual_state.read_text(encoding="utf-8", errors="replace").strip()
                if q_val in ("small", "cd", "hires", "ask"):
                    self.quality_mode = q_val

        auto_state = self.data_dir / "autoplay.state"
        auto_sig = self._file_sig(auto_state)
        if auto_sig != sigs.get("auto", ...):
            sigs["auto"] = auto_sig
            if auto_sig is not None:
                self.autoplay_on = auto_state.read_text(encoding="utf-8", errors="replace").strip() == "1"

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

    def cycle_catalog(self) -> str:
        cur = getattr(self, "catalog_source", "ytm")
        idx = self.CATALOG_SOURCES.index(cur) if cur in self.CATALOG_SOURCES else 0
        nxt = self.CATALOG_SOURCES[(idx + 1) % len(self.CATALOG_SOURCES)]
        self.catalog_source = nxt
        return nxt

    def cycle_search_type(self) -> str:
        cur = getattr(self, "search_type", "songs")
        idx = self.SEARCH_TYPES.index(cur) if cur in self.SEARCH_TYPES else 0
        nxt = self.SEARCH_TYPES[(idx + 1) % len(self.SEARCH_TYPES)]
        self.search_type = nxt
        return nxt

    def cycle_local_group(self) -> str:
        cur = getattr(self, "local_group_mode", "flat")
        idx = self.LOCAL_GROUP_MODES.index(cur) if cur in self.LOCAL_GROUP_MODES else 0
        nxt = self.LOCAL_GROUP_MODES[(idx + 1) % len(self.LOCAL_GROUP_MODES)]
        self.local_group_mode = nxt
        self.nav_stack = []
        self.drill_title = ""
        self.drill_ref = ""
        self.drill_kind = ""
        if self.active_tab == "local":
            self.selected_idx = 0
            self.scroll_top = 0
            self.load_tab_items()
        return nxt

    def push_nav(
        self,
        title: str,
        tracks: list[tuple[str, str, str, str]],
        context_ref: str = "",
        kind: str = "browse",
    ) -> None:
        stack = getattr(self, "nav_stack", None)
        if stack is None:
            stack = []
            self.nav_stack = stack
        stack.append(
            {
                "tracks": list(self.tracks),
                "selected_idx": self.selected_idx,
                "scroll_top": self.scroll_top,
                "drill_title": getattr(self, "drill_title", ""),
                "drill_ref": getattr(self, "drill_ref", ""),
                "drill_kind": getattr(self, "drill_kind", ""),
            }
        )
        self.tracks = list(tracks)
        self.selected_idx = 0
        self.scroll_top = 0
        self.drill_title = title
        self.drill_ref = context_ref
        self.drill_kind = kind

    def pop_nav(self) -> bool:
        stack = getattr(self, "nav_stack", None)
        if not stack:
            return False
        frame = stack.pop()
        self.tracks = list(frame.get("tracks") or [])
        self.selected_idx = int(frame.get("selected_idx") or 0)
        self.scroll_top = int(frame.get("scroll_top") or 0)
        self.drill_title = str(frame.get("drill_title") or "")
        self.drill_ref = str(frame.get("drill_ref") or "")
        self.drill_kind = str(frame.get("drill_kind") or "")
        return True

    def load_playlists_list(self) -> list[tuple[str, str, str, str]]:
        pl_dir = self.data_dir / "playlists"
        rows: list[tuple[str, str, str, str]] = []
        if pl_dir.is_dir():
            for p in sorted(pl_dir.glob("*.tsv"), key=lambda x: x.name.lower()):
                lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
                rows.append((f"pl:{p.stem}", p.stem, f"{len(lines)} tracks", "[PL]"))
        return rows

    def load_local_grouped(self) -> list[tuple[str, str, str, str]]:
        cache_file = self.cache_dir / "library.json"
        entries = library.scan_library(self.download_dir, cache_file)
        mode = getattr(self, "local_group_mode", "flat")
        groups_map: dict[str, list[dict[str, Any]]] = {}
        self._local_groups = groups_map

        if mode == "artist":
            rows: list[tuple[str, str, str, str]] = []
            for g in library.group_by_artist(entries):
                key = f"lart:{g['artist']}"
                groups_map[key] = g["tracks"]
                rows.append((key, g["artist"], f"{len(g['tracks'])} tracks", "[L-ART]"))
            return rows

        if mode == "album":
            rows = []
            for g in library.group_by_album(entries):
                key = f"lalb:{g['album']}"
                groups_map[key] = g["tracks"]
                rows.append((key, g["album"], f"{g['artist']} • {len(g['tracks'])} tracks", "[L-ALB]"))
            return rows

        rows = []
        for e in entries:
            artist = str(e.get("artist") or "").strip()
            title = str(e.get("title") or e.get("filename") or "")
            disp = f"{artist} - {title}" if artist and title else title
            meta_parts = [p for p in (str(e.get("album") or "").strip(), str(e.get("codec") or "").strip()) if p]
            rows.append((str(e.get("path") or ""), disp, " • ".join(meta_parts) or "Local Audio", "[LOC]"))
        return rows

    def _catalog_entity_to_row(self, ent: dict[str, Any]) -> tuple[str, str, str, str]:
        src = str(ent.get("source") or "ytm").lower()
        etype = str(ent.get("type") or "song").lower()
        eid = str(ent.get("id") or "")
        title = str(ent.get("title") or eid)
        artist = str(ent.get("artist") or "")
        album = str(ent.get("album") or "")
        dur = int(ent.get("duration") or 0)
        year = int(ent.get("year") or 0)

        parts: list[str] = []
        if artist:
            parts.append(artist)
        if album and etype == "song":
            parts.append(album)
        elif year > 0 and etype == "album":
            parts.append(str(year))
        if dur > 0:
            parts.append(format_mmss(dur))

        if etype == "song":
            row_id = eid if src == "ytm" else catalog.format_ref(src, "song", eid)
            tag = f"[{src.upper()}]"
        elif etype == "album":
            row_id = catalog.format_ref(src, "album", eid)
            tag = "[ALB]"
        elif etype == "artist":
            row_id = catalog.format_ref(src, "artist", eid)
            tag = "[ART]"
        else:
            row_id = catalog.format_ref(src, "playlist", eid)
            tag = "[PL]"
        return (row_id, title, " • ".join(parts), tag)

    def load_tab_items(self) -> None:
        self.refresh_local_files_meta()
        if getattr(self, "nav_stack", None):
            if self.selected_idx >= len(self.tracks):
                self.selected_idx = max(0, len(self.tracks) - 1)
            if self.scroll_top > self.selected_idx:
                self.scroll_top = self.selected_idx
            return

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
            self.tracks = self.load_local_grouped()
        elif self.active_tab == "playlists":
            self.tracks = self.load_playlists_list()

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
                        self.nav_stack = []
                        self.drill_title = ""
                        self.drill_ref = ""
                        self.drill_kind = ""
                        self.selected_idx = 0
                        self.scroll_top = 0
                        self.load_tab_items()
                    self.set_toast(f"Found {len(rows)} tracks")
                else:
                    self.set_toast("Search failed")
            elif kind == "browse_done":
                bseq, title, ref, rows, ok = payload
                if bseq != getattr(self, "browse_seq", 0):
                    continue
                if ok and rows:
                    self.push_nav(title, rows, context_ref=ref, kind="browse")
                    self.set_toast(f"Opened {title[:22]} ({len(rows)})")
                elif ok:
                    self.set_toast(f"Empty: {title[:24]}")
                else:
                    self.set_toast(f"Browse failed: {title[:20]}")
            elif kind == "dl_event":
                ev = dict(payload) if isinstance(payload, dict) else {}
                jobs = getattr(self, "dl_jobs", None)
                if jobs is None:
                    jobs = []
                    self.dl_jobs = jobs
                cid = str(ev.pop("_client_id", "") or "")
                jid = str(ev.get("job_id") or "")
                if cid and jid and cid != jid:
                    jobs[:] = [j for j in jobs if j.get("job_id") != cid]
                title = str(ev.get("title") or "download")
                status = str(ev.get("status") or "running")
                pct = int(ev.get("pct") or 0)
                idx = next((i for i, j in enumerate(jobs) if j.get("job_id") == jid and (not title or j.get("title") == title)), None)
                if idx is None and jid.startswith("dlm-"):
                    idx = next((i for i, j in enumerate(jobs) if j.get("job_id") == jid), None)
                if idx is not None:
                    jobs[idx].update(ev)
                else:
                    jobs.append(ev)
                    if len(jobs) > 20:
                        del jobs[:-20]
                if status == "done":
                    self.set_toast(f"DL Done: {title[:24]}")
                    if self.active_tab == "local" and not getattr(self, "nav_stack", None):
                        self.load_tab_items()
                elif status == "skipped":
                    self.set_toast(f"DL Cached: {title[:22]}")
                elif status == "error":
                    err = str(ev.get("error") or "failed")
                    self.set_toast(f"DL Err: {err[:24]}")
                elif pct > 0:
                    self.set_toast(f"DL {pct}%: {title[:22]}")
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

    def run_cli_async(
        self,
        args: list[str],
        toast: str = "",
        reload_after: bool = False,
        timeout: float | None = 30.0,
    ) -> None:
        def worker() -> None:
            msg = ""
            try:
                proc = subprocess.run(
                    [self.bin_path, *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
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

    def run_cli_batch_async(
        self,
        commands: list[list[str]],
        toast: str = "",
        reload_after: bool = False,
        timeout: float | None = 30.0,
    ) -> None:
        if not commands:
            return

        def worker() -> None:
            msg = toast
            try:
                for cmd_args in commands:
                    proc = subprocess.run(
                        [self.bin_path, *cmd_args],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout,
                    )
                    if proc.returncode != 0:
                        err_lines = (proc.stderr or proc.stdout or "Command failed").strip().splitlines()
                        err = err_lines[-1] if err_lines else "Command failed"
                        msg = re.sub(r"\x1b\[[0-9;]*m", "", err)[:42]
                        break
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
        self.nav_stack = []
        self.drill_title = ""
        self.drill_ref = ""
        self.drill_kind = ""
        self.search_loading = True
        self.search_focus = False
        self.search_seq += 1
        seq = self.search_seq
        cat_src = getattr(self, "catalog_source", "ytm")
        stype = getattr(self, "search_type", "songs")
        self.set_toast(f"Searching {cat_src}/{stype}: {q[:18]}...")

        def worker() -> None:
            rows: list[tuple[str, str, str, str]] = []
            ok = False
            try:
                proc = subprocess.run(
                    [self.bin_path, "search", "--catalog", cat_src, "--type", stype, "--json", q],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    items = json.loads(proc.stdout)
                    if isinstance(items, list):
                        for ent in items:
                            if isinstance(ent, dict) and ent.get("id"):
                                rows.append(self._catalog_entity_to_row(ent))
                        ok = True
            except Exception:
                ok = False
            finally:
                self.ui_events.put(("search_done", (seq, rows, ok)))

        threading.Thread(target=worker, daemon=True).start()

    def trigger_browse(self, ref: str, title: str) -> None:
        self.browse_seq = getattr(self, "browse_seq", 0) + 1
        bseq = self.browse_seq
        self.set_toast(f"Loading {title[:24]}...")

        def worker() -> None:
            rows: list[tuple[str, str, str, str]] = []
            ok = False
            try:
                proc = subprocess.run(
                    [self.bin_path, "browse", "--json", ref],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    for it in data.get("items") or []:
                        if isinstance(it, dict) and it.get("id"):
                            rows.append(self._catalog_entity_to_row(it))
                    ok = True
            except Exception:
                ok = False
            finally:
                self.ui_events.put(("browse_done", (bseq, title, ref, rows, ok)))

        threading.Thread(target=worker, daemon=True).start()

    def ensure_art_async(self, video_id: str, art_w: int = 22, art_h: int = 10) -> None:
        if not self.art_enabled or not VIDEO_ID_RE.match(video_id):
            return
        cache_key = f"{video_id}:{art_w}x{art_h}"
        with self.lock:
            if cache_key in self.art_cache or cache_key in self.art_inflight:
                return
            self.art_inflight.add(cache_key)

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
                            f"--size={art_w}x{art_h}",
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
                        self.art_cache[cache_key] = parsed
            except Exception:
                pass
            finally:
                with self.lock:
                    self.art_inflight.discard(cache_key)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_lyrics_async(self, title: str, track_key: str, artist: str = "", duration: float = 0.0) -> None:
        clean_title = normalize_track_title(title)
        if not clean_title.strip():
            return
        self.lyrics_track_key = track_key
        self.lyrics_loading = True
        lbl = f"{clean_title} - {artist}" if artist else clean_title
        self.lyrics_header = f"Loading lyrics: {lbl[:36]}..."
        self.lyrics_lines = []

        def worker() -> None:
            header = f"No matching lyrics: {lbl}"
            lines: list[tuple[float | None, str]] = []
            try:
                res = fetch_lyrics(clean_title, artist=artist, duration=duration)
                if res:
                    src_tag = "LRCLIB" if res.get("source") == "lrclib" else "Fallback"
                    header = f"{res['title']} - {res['artist']} [{src_tag}]".strip(" -")
                    if res.get("synced_lines"):
                        lines = [(t, txt) for t, txt in res["synced_lines"]]
                    else:
                        lines = [(None, ln) for ln in str(res.get("plain_text") or "").splitlines()]
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
                self.lyrics_track_key = ""
                self.lyrics_lines = []
                self.lyrics_loading = False
                self.lyrics_header = ""
                self.drawer_scroll = 0
                self.drawer_sel = 0
                np_rect = getattr(self, "last_layout", {}).get("now_playing")
                art_sz = calc_art_size(np_rect[1], np_rect[3]) if np_rect else (22, 10)
                if art_sz:
                    self.ensure_art_async(vid, art_sz[0], art_sz[1])
                if self.active_tab == "queue":
                    self.load_tab_items()

            if not idle and cur_path and self.right_tab == "lyrics" and self.lyrics_track_key != cur_path and not self.lyrics_loading:
                if is_placeholder_title(title, vid):
                    self.lyrics_header = "Waiting for track metadata..."
                else:
                    self.fetch_lyrics_async(title, cur_path, artist=artist, duration=dur)
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
        brand = " MUSICCTL v2.2 "
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
        cx = 1
        cat_src = getattr(self, "catalog_source", "ytm")
        stype = getattr(self, "search_type", "songs")
        cx = self.draw_button(1, cx, f"C:{cat_src}", "cycle_catalog", None, active=(cat_src != "ytm"))
        if w >= 112:
            for scope_k, scope_lbl in (("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists"), ("playlists", "Lists")):
                attr = curses.color_pair(PAIR_PILL_ACTIVE if stype == scope_k else PAIR_PILL_BTN) | curses.A_BOLD
                btn = f"[{scope_lbl}]"
                self.safe_addstr(1, cx, btn, attr)
                self.add_hitbox(cx, 1, cx + len(btn), 2, "set_search_type", scope_k)
                cx += len(btn) + 1
        else:
            cx = self.draw_button(1, cx, f"T:{stype}", "cycle_search_type", None, active=(stype != "songs"))

        btn_search_w = 8
        btn_clear_w = 7
        box_x = cx
        box_w = max(10, w - box_x - btn_search_w - btn_clear_w - 4)
        if self.search_focus or self.search_query:
            cursor_ch = "_" if self.search_focus else ""
            inner = truncate_width(self.search_query + cursor_ch, max(1, box_w - 2))
            box_txt = " " + inner + " " * max(0, box_w - 1 - display_width(inner))
            box_attr = curses.color_pair(PAIR_INPUT) | (curses.A_BOLD if self.search_focus else 0)
        else:
            ph = truncate_width("Press / to search...", max(1, box_w - 2))
            box_txt = " " + ph + " " * max(0, box_w - 1 - display_width(ph))
            box_attr = curses.color_pair(PAIR_DIM)
        self.safe_addstr(1, box_x, box_txt, box_attr)
        self.add_hitbox(box_x, 1, box_x + box_w, 2, "focus_search", None)

        bx = box_x + box_w + 1
        search_lbl = "..." if self.search_loading else "Search"
        bx = self.draw_button(1, bx, search_lbl, "do_search", None, active=self.search_loading)
        self.draw_button(1, bx, "Clear", "clear_search", None)

    def render_left_pane(self, y0: int, h: int, x0: int, w: int) -> None:
        focused = getattr(self, "focus_pane", "list") == "list"
        border_attr = curses.color_pair(PAIR_CYAN if focused else PAIR_BOX) | (curses.A_BOLD if focused else 0)
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", border_attr)
        stack = getattr(self, "nav_stack", [])
        if stack:
            bx = x0 + 2
            bx = self.draw_button(y0, bx, "Bksp <-", "nav_back", None)
            title_lbl = truncate_width(getattr(self, "drill_title", "") or "Items", max(8, w - bx - 28))
            hdr = f" {title_lbl} ({len(self.tracks)}) "
            self.safe_addstr(y0, bx, hdr, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            ax = bx + display_width(hdr) + 1
            if ax + 24 < x0 + w - 1:
                ax = self.draw_button(y0, ax, "P Play All", "play_all", None)
                ax = self.draw_button(y0, ax, "E +Q All", "add_all", None)
                ax = self.draw_button(y0, ax, "W DL All", "dl_all", None)
                if getattr(self, "drill_kind", "") == "playlist" and ax + 9 < x0 + w - 1:
                    self.draw_button(y0, ax, "S Sync", "sync_pl", None)
        else:
            tab_title = dict(self.TABS).get(self.active_tab, self.active_tab.upper())
            hdr = f" {tab_title} ({len(self.tracks)}) "
            self.safe_addstr(y0, x0 + 2, hdr, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            if self.active_tab == "local":
                lmode = getattr(self, "local_group_mode", "flat")
                lbl = "L Artists" if lmode == "artist" else ("L Albums" if lmode == "album" else "L Flat")
                lx = x0 + 2 + display_width(hdr) + 1
                if lx + len(lbl) + 4 < x0 + w - 1:
                    self.draw_button(y0, lx, lbl, "cycle_local_group", None, active=(lmode != "flat"))
            elif self.active_tab == "playlists" and self.tracks:
                px = x0 + 2 + display_width(hdr) + 1
                if px + 9 < x0 + w - 1:
                    self.draw_button(y0, px, "S Sync", "sync_pl", None)

        body_h = max(1, h - 2)
        self.last_list_body_h = body_h
        if self.selected_idx < self.scroll_top:
            self.scroll_top = self.selected_idx
        elif self.selected_idx >= self.scroll_top + body_h:
            self.scroll_top = self.selected_idx - body_h + 1

        for row_i in range(body_h):
            ry = y0 + 1 + row_i
            self.safe_addstr(ry, x0, "│", border_attr)
            self.safe_addstr(ry, x0 + w - 1, "│", border_attr)
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
                text = format_track_row_columns(title, meta, rem_w)
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
                self.add_hitbox(x0 + 1, ry, x0 + w - 1, ry + 1, "focus_pane", "list")

        self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", border_attr)

    def render_now_playing_card(self, y0: int, h: int, x0: int, w: int) -> None:
        if w < 24 or h < 4:
            return
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", curses.color_pair(PAIR_BOX))
        self.safe_addstr(y0, x0 + 2, " NOW PLAYING ", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        for r in range(1, h - 1):
            self.safe_addstr(y0 + r, x0, "│", curses.color_pair(PAIR_BOX))
            self.safe_addstr(y0 + r, x0 + w - 1, "│", curses.color_pair(PAIR_BOX))
        self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", curses.color_pair(PAIR_BOX))

        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        paused = bool(self.mpv_state.get("pause", False))
        cur_path = str(self.mpv_state.get("path") or "")
        cur_vid = extract_video_id(cur_path) if cur_path else ""
        sel_vid = self.tracks[self.selected_idx][0] if self.tracks and self.selected_idx < len(self.tracks) else ""
        art_vid = cur_vid if VIDEO_ID_RE.match(cur_vid) else sel_vid

        art_rows: list[list[tuple[str, int, int]]] = []
        art_sz = calc_art_size(h, w)
        if self.art_enabled and VIDEO_ID_RE.match(art_vid) and art_sz is not None:
            aw, ah = art_sz
            cache_key = f"{art_vid}:{aw}x{ah}"
            self.ensure_art_async(art_vid, aw, ah)
            with self.lock:
                art_rows = self.art_cache.get(cache_key, [])

        info_x = x0 + 2
        if art_rows and art_sz is not None:
            self.reset_art_pairs_for_vid(art_vid)
            art_w = min(art_sz[0], max(len(r) for r in art_rows))
            for ri, row_cells in enumerate(art_rows[: max(1, h - 2)]):
                for ci, (ch, fg, bg) in enumerate(row_cells[:art_w]):
                    try:
                        self.stdscr.addstr(y0 + 1 + ri, x0 + 2 + ci, ch, curses.color_pair(self.get_art_pair(fg, bg)))
                    except curses.error:
                        pass
            info_x = x0 + 3 + art_w

        info_w = max(8, x0 + w - 2 - info_x)
        if not idle:
            title = str(self.mpv_state.get("media-title") or self.mpv_state.get("filename") or "Unknown")
            meta_fields = extract_metadata_fields(self.mpv_state.get("metadata"))
            artist = meta_fields["artist"]
            alb_lbl = format_album_line(meta_fields["album"], meta_fields["year"])
            codec = str(self.mpv_state.get("audio-codec-name") or "-")
            srate = self.mpv_state.get("audio-params/samplerate") or 0
            sfmt = str(self.mpv_state.get("audio-params/format") or "")
            audio_lbl = format_audio_badge(codec, srate, sfmt) or codec
            qpos = int(self.mpv_state.get("playlist-pos") or 0) + 1
            qcnt = int(self.mpv_state.get("playlist-count") or 1)
            spd_lbl = format_speed(self.mpv_state.get("speed"))
            loop_mode = self.get_loop_mode()

            head_txt = title if (not artist or h > 5) else f"{title} — {artist}"
            self.safe_addstr(y0 + 1, info_x, truncate_width(head_txt, info_w), curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
            row_off = 2
            max_row = h - 2
            if artist and h > 5 and row_off < max_row:
                art_txt = f"by {artist}" if (not alb_lbl or h >= 8) else f"by {artist} • {alb_lbl}"
                self.safe_addstr(y0 + row_off, info_x, truncate_width(art_txt, info_w), curses.color_pair(PAIR_PURPLE) | curses.A_BOLD)
                row_off += 1
            if alb_lbl and h >= 8 and row_off < max_row:
                self.safe_addstr(y0 + row_off, info_x, truncate_width(f"Album: {alb_lbl}", info_w), curses.color_pair(PAIR_ICE))
                row_off += 1
            if row_off < max_row:
                self.safe_addstr(y0 + row_off, info_x, truncate_width(f"Audio: {audio_lbl}  |  Queue: {qpos}/{qcnt}", info_w), curses.color_pair(PAIR_CYAN))
                row_off += 1
            if h >= 7 and row_off < max_row:
                dsp_line = f"EQ: {self.eq_mode}  |  Norm: {'on' if self.norm_on else 'off'}  |  Loop: {loop_mode}"
                self.safe_addstr(y0 + row_off, info_x, truncate_width(dsp_line, info_w), curses.color_pair(PAIR_ICE))
                row_off += 1
            extra_parts: list[str] = []
            if spd_lbl:
                extra_parts.append(f"Speed: {spd_lbl}")
            if self.sleep_rem:
                extra_parts.append(f"Sleep: {self.sleep_rem}")
            if extra_parts and row_off < max_row:
                self.safe_addstr(y0 + row_off, info_x, truncate_width("  |  ".join(extra_parts), info_w), curses.color_pair(PAIR_AMBER))
                row_off += 1

            spec_y = y0 + h - 2
            if spec_y >= y0 + row_off:
                wave = build_lightweight_wave(info_w, self.tick, paused)
                self.safe_addstr(spec_y, info_x, wave, curses.color_pair(PAIR_DIM if paused else PAIR_SPEC_HIGH))
        else:
            self.safe_addstr(y0 + 1, info_x, "No active playback", curses.color_pair(PAIR_DIM) | curses.A_BOLD)
            if h >= 5:
                self.safe_addstr(y0 + 2, info_x, truncate_width("Enter / Double-click track to play | ? Help", info_w), curses.color_pair(PAIR_DIM))
            if h >= 6:
                self.safe_addstr(y0 + 3, info_x, truncate_width(f"EQ: {self.eq_mode}  |  Norm: {'on' if self.norm_on else 'off'}", info_w), curses.color_pair(PAIR_ICE))

    def render_drawer_card(self, y0: int, h: int, x0: int, w: int) -> None:
        if w < 24 or h < 4:
            return
        focused = getattr(self, "focus_pane", "list") == "drawer"
        border_attr = curses.color_pair(PAIR_CYAN if focused else PAIR_BOX) | (curses.A_BOLD if focused else 0)
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", border_attr)
        tx = x0 + 2
        hide_w = 9  # "[z Hide]" + spacing
        for rkey, rlbl in self.RIGHT_TABS:
            if tx + len(rlbl) + 4 >= x0 + w - hide_w:
                break
            tx = self.draw_button(y0, tx, rlbl, "rtab", rkey, active=(self.right_tab == rkey))
        if x0 + w - hide_w > tx:
            self.draw_button(y0, x0 + w - hide_w, "z Hide", "toggle_drawer", None)

        b_inner_h = max(1, h - 2)
        b_inner_w = max(1, w - 4)
        self.last_drawer_body_h = b_inner_h
        for r in range(b_inner_h):
            self.safe_addstr(y0 + 1 + r, x0, "│", border_attr)
            self.safe_addstr(y0 + 1 + r, x0 + w - 1, "│", border_attr)
        self.add_hitbox(x0 + 1, y0 + 1, x0 + w - 1, y0 + h - 1, "focus_pane", "drawer")

        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        cur_path = str(self.mpv_state.get("path") or "")
        cur_vid = extract_video_id(cur_path) if cur_path else ""
        d_scroll = max(0, getattr(self, "drawer_scroll", 0))

        if self.right_tab == "upnext":
            pl = self.mpv_state.get("playlist") or []
            if not pl:
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width("Queue empty. Press [a] or right-click a track to enqueue.", b_inner_w), curses.color_pair(PAIR_DIM))
                if b_inner_h >= 2:
                    self.safe_addstr(y0 + 2, x0 + 2, truncate_width("Tip: [r] Queue Radio mix | [z] Hide drawer | [v] Layout", b_inner_w), curses.color_pair(PAIR_DIM))
            else:
                cur_i = next((i for i, e in enumerate(pl) if e.get("current")), 0)
                if focused:
                    dsel = max(0, min(len(pl) - 1, getattr(self, "drawer_sel", 0)))
                    self.drawer_sel = dsel
                    if dsel < d_scroll:
                        d_scroll = dsel
                    elif dsel >= d_scroll + b_inner_h:
                        d_scroll = dsel - b_inner_h + 1
                    self.drawer_scroll = d_scroll
                    start_i = d_scroll
                else:
                    start_i = d_scroll if d_scroll > 0 else max(0, cur_i - 1)
                    dsel = -1
                rows_drawn = 0
                for row_i in range(b_inner_h):
                    qi = start_i + row_i
                    if qi >= len(pl):
                        break
                    entry = pl[qi]
                    is_c = bool(entry.get("current"))
                    is_dsel = focused and qi == dsel
                    qtitle = entry.get("title") or entry.get("filename") or "Unknown"
                    mark = "▸" if (is_dsel or is_c) else " "
                    line = truncate_width(f"{mark} {qi + 1:2d}. {qtitle}", b_inner_w)
                    if is_dsel:
                        attr = curses.color_pair(PAIR_SEL) | curses.A_BOLD
                    elif is_c:
                        attr = curses.color_pair(PAIR_GREEN) | curses.A_BOLD
                    else:
                        attr = curses.color_pair(PAIR_DEFAULT)
                    self.safe_addstr(y0 + 1 + row_i, x0 + 2, line, attr)
                    self.add_hitbox(x0 + 2, y0 + 1 + row_i, x0 + w - 1, y0 + 2 + row_i, "jump", qi)
                    rows_drawn += 1
                if len(pl) <= 1 and rows_drawn < b_inner_h:
                    self.safe_addstr(y0 + 1 + rows_drawn, x0 + 2, truncate_width("Tip: [a] enqueue track | [r] queue Radio | [z] hide drawer", b_inner_w), curses.color_pair(PAIR_DIM))
        elif self.right_tab == "lyrics":
            if self.lyrics_loading:
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_CYAN))
            elif not self.lyrics_lines:
                hdr_msg = self.lyrics_header or "Play a track to auto-load lyrics"
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width(hdr_msg, b_inner_w), curses.color_pair(PAIR_DIM))
                if not idle and b_inner_h >= 2:
                    mf = extract_metadata_fields(self.mpv_state.get("metadata"))
                    ctx_bits = [b for b in (mf.get("artist"), format_album_line(mf.get("album", ""), mf.get("year", 0))) if b]
                    if ctx_bits:
                        self.safe_addstr(y0 + 2, x0 + 2, truncate_width(" • ".join(ctx_bits), b_inner_w), curses.color_pair(PAIR_ICE))
                if b_inner_h >= 3:
                    self.safe_addstr(y0 + 3, x0 + 2, truncate_width("Press [r] Radio, [I] Info, or [z] Hide drawer", b_inner_w), curses.color_pair(PAIR_DIM))
            else:
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_PURPLE) | curses.A_BOLD)
                tpos = float(self.mpv_state.get("time-pos") or 0.0)
                active_l = 0
                if self.lyrics_lines[0][0] is not None:
                    for li, (ts, _txt) in enumerate(self.lyrics_lines):
                        if ts is not None and ts <= tpos + 0.15:
                            active_l = li
                        else:
                            break
                body_rows = max(0, b_inner_h - 1)
                if d_scroll > 0:
                    start_l = min(max(0, len(self.lyrics_lines) - max(1, body_rows)), d_scroll)
                else:
                    start_l = max(0, active_l - max(1, body_rows // 3))
                for row_i in range(body_rows):
                    li = start_l + row_i
                    if li >= len(self.lyrics_lines):
                        break
                    ts, ltxt = self.lyrics_lines[li]
                    is_cur_l = ts is not None and li == active_l
                    prefix = "▸ " if is_cur_l else "  "
                    attr = curses.color_pair(PAIR_LYRIC_ACTIVE if is_cur_l else (PAIR_DIM if ts is not None and li < active_l else PAIR_DEFAULT))
                    if is_cur_l:
                        attr |= curses.A_BOLD
                    self.safe_addstr(y0 + 2 + row_i, x0 + 2, truncate_width(prefix + ltxt, b_inner_w), attr)
        elif self.right_tab == "downloads":
            jobs = list(reversed(getattr(self, "dl_jobs", [])))
            if not jobs:
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width("No active or recent downloads. Press [d] on a track.", b_inner_w), curses.color_pair(PAIR_DIM))
            else:
                if b_inner_h >= 2:
                    self.draw_button(y0 + 1, x0 + 2, "X Cancel All", "dl_cancel", None)
                body_rows = max(0, b_inner_h - 1)
                start_j = min(max(0, len(jobs) - max(1, body_rows)), d_scroll)
                for row_i in range(body_rows):
                    ji = start_j + row_i
                    if ji >= len(jobs):
                        break
                    j = jobs[ji]
                    st = str(j.get("status") or "running")
                    pct = int(j.get("pct") or 0)
                    badge = "[DONE]" if st == "done" else ("[SKIP]" if st == "skipped" else ("[ERR]" if st == "error" else f"[{pct:3d}%]"))
                    pair_j = PAIR_GREEN if st == "done" else (PAIR_DIM if st == "skipped" else (PAIR_RED if st == "error" else PAIR_CYAN))
                    line = truncate_width(f"{badge} {j.get('title') or ''} ({j.get('stage') or ''})", b_inner_w)
                    self.safe_addstr(y0 + 2 + row_i, x0 + 2, line, curses.color_pair(pair_j))
        else:
            if self.tracks and 0 <= self.selected_idx < len(self.tracks):
                svid, stitle, smeta, stag = self.tracks[self.selected_idx]
                if not idle and svid and (svid == cur_vid or svid == cur_path):
                    mf = extract_metadata_fields(self.mpv_state.get("metadata"))
                    sel_alb = format_album_line(mf["album"], mf["year"])
                else:
                    sel_alb = extract_album_from_meta(smeta)
                info_rows: list[tuple[str, int]] = [
                    (f"Title: {stitle}", curses.color_pair(PAIR_TITLE) | curses.A_BOLD),
                ]
                if sel_alb:
                    info_rows.append((f"Album: {sel_alb}", curses.color_pair(PAIR_ICE)))
                info_rows.append((f"Info : {smeta or '-'}", curses.color_pair(PAIR_ICE)))
                info_rows.append((f"ID   : {svid} {stag}", curses.color_pair(PAIR_PURPLE)))
                max_text_rows = max(1, b_inner_h - 1)
                start_r = min(max(0, len(info_rows) - max_text_rows), d_scroll)
                drawn = 0
                for ri in range(start_r, min(len(info_rows), start_r + max_text_rows)):
                    txt, attr = info_rows[ri]
                    self.safe_addstr(y0 + 1 + drawn, x0 + 2, truncate_width(txt, b_inner_w), attr)
                    drawn += 1
                if b_inner_h >= drawn + 1:
                    ax = x0 + 2
                    ay = y0 + 1 + drawn
                    ax = self.draw_button(ay, ax, "Ent Play", "play_sel")
                    ax = self.draw_button(ay, ax, "a +Q", "add_sel")
                    ax = self.draw_button(ay, ax, "r Radio", "radio_sel")
                    if ax + 9 < x0 + w - 1:
                        ax = self.draw_button(ay, ax, "f Fav", "fav_sel")
                    if ax + 8 < x0 + w - 1:
                        ax = self.draw_button(ay, ax, "d DL", "dl_sel")
                    if stag == "[Q]" and b_inner_h >= drawn + 2:
                        qx = x0 + 2
                        qx = self.draw_button(ay + 1, qx, "K Up", "q_up")
                        self.draw_button(ay + 1, qx, "J Down", "q_down")
            else:
                self.safe_addstr(y0 + 1, x0 + 2, "No track selected", curses.color_pair(PAIR_DIM))

        self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", border_attr)

    def render_right_pane(self, y0: int, h: int, x0: int, w: int) -> None:
        if w < 24 or h < 8:
            return
        top_h = min(15, max(8, int(h * 0.36)))
        bot_h = max(4, h - top_h)
        self.render_now_playing_card(y0, top_h, x0, w)
        self.render_drawer_card(y0 + top_h, bot_h, x0, w)

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
        bx = self.draw_button(y0 + 2, bx, "b Prev", "prev")
        play_lbl = "Spc Play" if (idle or paused) else "Spc Pause"
        bx = self.draw_button(y0 + 2, bx, play_lbl, "toggle", active=(not idle and not paused))
        bx = self.draw_button(y0 + 2, bx, "n Next", "next")
        bx = self.draw_button(y0 + 2, bx, "x Stop", "stop")
        if bx + 28 < w:
            bx = self.draw_button(y0 + 2, bx, "r Radio", "radio_sel")
            bx = self.draw_button(y0 + 2, bx, "f Fav", "fav_sel")
        if bx + 20 < w:
            bx = self.draw_button(y0 + 2, bx, "a +Q", "add_sel")

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
        dx = self.draw_button(y0 + 3, dx, f"o Loop:{loop_m}", "loop", active=(loop_m != "off"))
        dx = self.draw_button(y0 + 3, dx, "u Shuf", "shuffle")
        dx = self.draw_button(y0 + 3, dx, f"Q:{self.quality_mode}", "quality", active=(self.quality_mode != "cd"))
        dx = self.draw_button(y0 + 3, dx, f"e EQ:{self.eq_mode}", "eq", active=(self.eq_mode != "flat"))
        dx = self.draw_button(y0 + 3, dx, f"N Norm:{'on' if self.norm_on else 'off'}", "norm", active=self.norm_on)
        if dx + 14 < w:
            dx = self.draw_button(y0 + 3, dx, f"A Auto:{'on' if self.autoplay_on else 'off'}", "autoplay", active=self.autoplay_on)
        dx = self.draw_button(y0 + 3, dx, "m Mute" if not muted else "m UNMUTE", "mute", active=muted, pair=PAIR_RED if muted else PAIR_PILL_BTN)
        eff_view = getattr(self, "last_layout", {}).get("effective") or getattr(self, "layout_mode", "auto")
        if dx + 15 < w:
            dx = self.draw_button(y0 + 3, dx, f"v {eff_view}", "cycle_layout", active=(getattr(self, "layout_mode", "auto") != "auto"))
        dx = self.draw_button(y0 + 3, dx, "? Help", "help", active=self.show_help)

        if self.toast_msg and time.monotonic() < self.toast_until and dx + 6 < w:
            t_str = f" {truncate_width(self.toast_msg, w - dx - 3)} "
            tx = max(dx + 1, w - len(t_str) - 1)
            self.safe_addstr(y0 + 3, tx, t_str, curses.color_pair(PAIR_TOAST) | curses.A_BOLD)

    def render_help_modal(self, h: int, w: int) -> None:
        lines = [
            "MUSICCTL v2.3 - HYBRID MOUSE + KEYBOARD CONTROLS",
            "",
            "Mouse:",
            "  Click / Double / Right  Select / Play or Open / Enqueue track",
            "  Click bars / tabs       Seek, set Volume, switch views & focus",
            "  Mouse wheel             Scroll list or drawer under cursor",
            "",
            "Layout & Navigation:",
            "  h / l or Left / Right   Focus Left List / Right Drawer",
            "  v / z                   Cycle Layout (auto/split/stacked/focus) / Hide Drawer",
            "  1-7 / U Y I D / Tab     Switch left tabs / Drawer tabs / Cycle (Shift-Tab rev)",
            "  j/k PgUp/PgDn g/G       Move/page cursor in active focused pane",
            "  / or Ctrl-S (Ctrl-U clr)Focus search | C Cat  T Type  L LocalGroup  Bksp Back",
            "",
            "Playback, Batch & Queue:",
            "  Enter / a / f / r / d   Play or Jump / Enqueue / Fav / Radio / Download",
            "  P / E / W / S / X       Play All / +Q All / DL All / Sync Pl / Cancel DL",
            "  J / K (on Queue tab)    Move selected queue track down / up",
            "  Space  n/b  o  u  Q     Pause/Play | Next/Prev | Loop | Shuffle | Quality",
            "  e EQ  N Norm  A Auto  m Mute | +/- Vol | [/] Seek | ? Help | q Quit",
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

        layout = compute_layout(h, w, getattr(self, "layout_mode", "auto"), getattr(self, "show_drawer", True))
        self.last_layout = layout
        if layout["drawer"] is None and getattr(self, "focus_pane", "list") == "drawer":
            self.focus_pane = "list"

        if layout["now_playing"] is not None:
            self.render_now_playing_card(*layout["now_playing"])
        self.render_left_pane(*layout["list"])
        if layout["drawer"] is not None:
            self.render_drawer_card(*layout["drawer"])

        deck_h = 4
        self.render_transport_deck(h - deck_h, w)
        if self.show_help:
            self.render_help_modal(h, w)
        self.stdscr.refresh()

    def _local_group_to_rows(self, key: str) -> list[tuple[str, str, str, str]]:
        tracks = getattr(self, "_local_groups", {}).get(key) or []
        rows: list[tuple[str, str, str, str]] = []
        for e in tracks:
            artist = str(e.get("artist") or "").strip()
            title = str(e.get("title") or e.get("filename") or "")
            trk = int(e.get("track") or 0)
            disp = f"{trk:02d}. {title}" if trk > 0 else title
            meta_parts = [p for p in (artist, str(e.get("album") or "").strip(), str(e.get("codec") or "").strip()) if p]
            rows.append((str(e.get("path") or ""), disp, " • ".join(meta_parts) or "Local Audio", "[LOC]"))
        return rows

    def _collect_track_refs(self) -> list[str]:
        refs: list[str] = []
        for vid, _t, _m, tag in self.tracks:
            if vid and tag not in ("[ALB]", "[ART]", "[PL]", "[L-ART]", "[L-ALB]"):
                refs.append(vid)
        return refs

    def play_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        if tag in ("[L-ART]", "[L-ALB]"):
            rows = self._local_group_to_rows(vid)
            self.push_nav(title, rows, context_ref=vid, kind="local_group")
            return
        if vid.startswith("pl:"):
            pl_name = vid[3:]
            rows = self.read_tsv(self.data_dir / "playlists" / f"{pl_name}.tsv", "[TRK]")
            self.push_nav(pl_name, rows, context_ref=vid, kind="playlist")
            return
        if tag in ("[ALB]", "[ART]", "[PL]") or re.match(r"^(?:ytm|deezer|itunes):(?:album|artist|playlist):", vid):
            self.trigger_browse(vid, title)
            return
        if tag == "[Q]":
            self.mpv.send(["set_property", "playlist-pos", self.selected_idx])
            self.mpv.send(["set_property", "pause", False])
            self.set_toast(f"Jump #{self.selected_idx + 1}")
        elif re.match(r"^(?:ytm|deezer|itunes):(?:song|track):", vid):
            self.run_cli_async(["play", vid], toast=f"Playing: {title[:24]}", reload_after=True)
        else:
            line = f"{vid}\t{title}\t{meta}\t{tag}"
            self.run_cli_async(["_play_line", line], toast=f"Playing: {title[:24]}", reload_after=True)

    def enqueue_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        if tag in ("[L-ART]", "[L-ALB]") or vid.startswith(("lart:", "lalb:")):
            paths = [str(e.get("path") or "") for e in getattr(self, "_local_groups", {}).get(vid, []) if e.get("path")]
            if paths:
                self.run_cli_batch_async([["add", p] for p in paths], toast=f"Queued {len(paths)} tracks", reload_after=True)
            return
        if vid.startswith("pl:") or tag in ("[ALB]", "[ART]", "[PL]") or re.match(r"^(?:ytm|deezer|itunes):(?:song|track|album|artist|playlist):", vid):
            self.run_cli_async(["add", vid], toast=f"Queued: {title[:24]}", reload_after=True)
            return
        line = f"{vid}\t{title}\t{meta}\t{tag}"
        self.run_cli_async(["_add_line", line], toast=f"Queued: {title[:24]}", reload_after=True)

    def handle_action(self, action: str, payload: Any, bstate: int = 0) -> None:
        if action == "close_help":
            self.show_help = False
            return
        if action == "tab":
            self.search_focus = False
            self.active_tab = payload
            self.nav_stack = []
            self.drill_title = ""
            self.drill_ref = ""
            self.drill_kind = ""
            self.selected_idx = 0
            self.scroll_top = 0
            self.load_tab_items()
        elif action == "cycle_catalog":
            nxt = self.cycle_catalog()
            self.set_toast(f"Catalog: {nxt}")
        elif action == "cycle_search_type":
            nxt = self.cycle_search_type()
            self.set_toast(f"Type: {nxt}")
        elif action == "set_search_type":
            if payload in self.SEARCH_TYPES:
                self.search_type = payload
                self.set_toast(f"Type: {payload}")
        elif action == "cycle_local_group":
            nxt = self.cycle_local_group()
            self.set_toast(f"Local view: {nxt}")
        elif action == "nav_back":
            self.pop_nav()
        elif action == "play_all":
            ctx = getattr(self, "drill_ref", "")
            refs = self._collect_track_refs()
            if ctx and not ctx.startswith(("lart:", "lalb:")):
                self.run_cli_async(["play", ctx], toast=f"Playing {self.drill_title[:20]}", reload_after=True)
            elif refs:
                cmds = [["play", refs[0]], *(["add", r] for r in refs[1:])]
                self.run_cli_batch_async(cmds, toast=f"Playing {len(refs)} tracks", reload_after=True)
        elif action == "add_all":
            ctx = getattr(self, "drill_ref", "")
            refs = self._collect_track_refs()
            if ctx and not ctx.startswith(("lart:", "lalb:")):
                self.run_cli_async(["add", ctx], toast=f"Queued {self.drill_title[:20]}", reload_after=True)
            elif refs:
                cmds = [["add", r] for r in refs]
                self.run_cli_batch_async(cmds, toast=f"Queued {len(refs)} tracks", reload_after=True)
        elif action == "dl_all":
            ctx = getattr(self, "drill_ref", "")
            if ctx and not ctx.startswith(("lart:", "lalb:")) and getattr(self, "dl_manager", None):
                self.dl_manager.start([ctx], label=self.drill_title or ctx)
                self.right_tab = "downloads"
        elif action == "sync_pl":
            target = ""
            if getattr(self, "drill_kind", "") == "playlist" and getattr(self, "drill_ref", "").startswith("pl:"):
                target = self.drill_ref
            elif self.active_tab == "playlists" and self.tracks and 0 <= self.selected_idx < len(self.tracks):
                cand = self.tracks[self.selected_idx][0]
                if cand.startswith("pl:"):
                    target = cand
            if target:
                pl_name = target[3:]
                if getattr(self, "dl_manager", None):
                    self.dl_manager.start(["sync", target], label=f"Sync {pl_name}")
                    self.right_tab = "downloads"
                    self.set_toast(f"Syncing {pl_name}...")
                else:
                    self.run_cli_async(["dl", "sync", target], toast=f"Synced {pl_name}", reload_after=True, timeout=300.0)
        elif action == "dl_cancel":
            self.focus_pane = "drawer"
            if getattr(self, "dl_manager", None):
                self.dl_manager.cancel_all()
                self.set_toast("Cancelling downloads...")
        elif action == "cycle_layout":
            nxt = self.cycle_layout_mode()
            self.set_toast(f"Layout: {nxt}")
        elif action == "toggle_drawer":
            shown = self.toggle_drawer()
            self.set_toast("Drawer: shown" if shown else "Drawer: hidden")
        elif action == "focus_pane":
            if payload in ("list", "drawer"):
                self.focus_pane = payload
        elif action == "rtab":
            self.right_tab = payload
            self.drawer_scroll = 0
            self.show_drawer = True
            self.focus_pane = "drawer"
            if payload == "lyrics":
                title = str(self.mpv_state.get("media-title") or "")
                cur_path = str(self.mpv_state.get("path") or "")
                artist = extract_primary_artist(self.mpv_state.get("metadata"))
                dur = float(self.mpv_state.get("duration") or 0.0)
                if cur_path and cur_path != self.lyrics_track_key and not self.lyrics_loading:
                    if is_placeholder_title(title, extract_video_id(cur_path)):
                        self.lyrics_header = "Waiting for track metadata..."
                    else:
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
            self.focus_pane = "list"
            self.selected_idx = int(payload)
            if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                self.play_selected()
            elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
                self.enqueue_selected()
        elif action == "jump":
            self.focus_pane = "drawer"
            self.drawer_sel = int(payload)
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
        elif action == "autoplay":
            self.run_cli_async(["autoplay", "toggle"], toast="Autoplay toggled", reload_after=True)
        elif action == "mute":
            self.run_cli_async(["mute"], toast="Mute toggled", reload_after=True)
        elif action == "help":
            self.show_help = not self.show_help
        elif action == "play_sel":
            self.play_selected()
        elif action == "add_sel":
            self.enqueue_selected()
        elif action == "radio_sel":
            self.set_toast("Queueing YTM Radio...")
            if self.tracks and self.selected_idx < len(self.tracks):
                vid = self.tracks[self.selected_idx][0]
                if VIDEO_ID_RE.match(vid):
                    self.run_cli_async(["radio", vid], toast="YTM Radio queued", reload_after=True)
                    return
            self.run_cli_async(["radio", "current"], toast="YTM Radio queued", reload_after=True)
        elif action == "fav_sel":
            if self.tracks and self.selected_idx < len(self.tracks):
                vid, title, meta, _tag = self.tracks[self.selected_idx]
                if vid in self.fav_ids:
                    self.run_cli_async(["fav", "rm", vid], toast="Removed favorite", reload_after=True)
                else:
                    self.run_cli_async(["fav", "add", vid, title, meta], toast="Added favorite", reload_after=True)
        elif action == "dl_sel":
            if self.tracks and self.selected_idx < len(self.tracks):
                vid, title, _m, tag = self.tracks[self.selected_idx]
                if tag in ("[L-ART]", "[L-ALB]", "[LOC]") or vid.startswith(("lart:", "lalb:")):
                    self.set_toast("Already local")
                    return
                if getattr(self, "dl_manager", None):
                    self.dl_manager.start([vid], label=title or vid)
                    self.set_toast(f"Downloading ({self.quality_mode}): {title[:20]}")
                else:
                    self.set_toast(f"Downloading ({self.quality_mode})...")
                    self.run_cli_async(["download", vid], toast="Download complete", reload_after=True, timeout=300.0)

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

    def cycle_layout_mode(self) -> str:
        cur = getattr(self, "layout_mode", "auto")
        idx = LAYOUT_MODES.index(cur) if cur in LAYOUT_MODES else 0
        nxt = LAYOUT_MODES[(idx + 1) % len(LAYOUT_MODES)]
        self.layout_mode = nxt
        self.show_drawer = nxt != "focus"
        if not self.show_drawer and getattr(self, "focus_pane", "list") == "drawer":
            self.focus_pane = "list"
        return nxt

    def toggle_drawer(self) -> bool:
        cur = bool(getattr(self, "show_drawer", True))
        nxt = not cur
        self.show_drawer = nxt
        if nxt and getattr(self, "layout_mode", "auto") == "focus":
            self.layout_mode = "auto"
        elif not nxt and getattr(self, "focus_pane", "list") == "drawer":
            self.focus_pane = "list"
        return nxt

    def _drawer_content_len(self) -> int:
        rtab = getattr(self, "right_tab", "upnext")
        if rtab == "upnext":
            return len((getattr(self, "mpv_state", {}) or {}).get("playlist") or [])
        if rtab == "lyrics":
            return len(getattr(self, "lyrics_lines", []) or [])
        if rtab == "downloads":
            return len(getattr(self, "dl_jobs", []) or [])
        return 6

    def scroll_active_pane(self, delta: int, *, pane: str | None = None) -> None:
        target = pane or getattr(self, "focus_pane", "list")
        if target == "drawer":
            rtab = getattr(self, "right_tab", "upnext")
            total = self._drawer_content_len()
            if rtab == "upnext":
                if total <= 0:
                    self.drawer_sel = 0
                else:
                    cur_s = getattr(self, "drawer_sel", 0)
                    self.drawer_sel = max(0, min(total - 1, cur_s + delta))
            else:
                body_h = max(1, getattr(self, "last_drawer_body_h", 8) - (1 if rtab in ("lyrics", "downloads") else 0))
                max_sc = max(0, total - body_h)
                cur_sc = getattr(self, "drawer_scroll", 0)
                self.drawer_scroll = max(0, min(max_sc, cur_sc + delta))
            return

        if self.tracks:
            self.selected_idx = max(0, min(len(self.tracks) - 1, self.selected_idx + delta))

    def activate_drawer_item(self) -> None:
        if getattr(self, "right_tab", "upnext") != "upnext":
            return
        pl = (getattr(self, "mpv_state", {}) or {}).get("playlist") or []
        if not pl:
            return
        idx = max(0, min(len(pl) - 1, getattr(self, "drawer_sel", 0)))
        self.mpv.send(["set_property", "playlist-pos", idx])
        self.mpv.send(["set_property", "pause", False])
        self.set_toast(f"Jump #{idx + 1}")

    def handle_key(self, wch: int | str) -> bool:
        if self.show_help:
            self.show_help = False
            return True

        if self.search_focus:
            self.search_query, act = apply_search_key(self.search_query, wch)
            if act == "blur":
                self.search_focus = False
            elif act == "submit":
                self.trigger_search(self.search_query)
            return True

        ch = ord(wch) if isinstance(wch, str) and len(wch) == 1 else wch

        if ch in (ord("q"), 3):
            return False
        if ch in (ord("/"), 19):  # / or Ctrl-S
            self.search_focus = True
        elif ch == 12:  # Ctrl-L clears search query
            self.search_query = ""
            self.set_toast("Search cleared")
        elif ch == ord("?"):
            self.show_help = True
        elif ch in (ord("h"), curses.KEY_LEFT):
            self.focus_pane = "list"
        elif ch in (ord("l"), curses.KEY_RIGHT):
            if not getattr(self, "show_drawer", True) or getattr(self, "layout_mode", "auto") == "focus":
                self.toggle_drawer()
            if getattr(self, "last_layout", {}).get("drawer") is not None or getattr(self, "show_drawer", True):
                self.focus_pane = "drawer"
        elif ch in (ord("j"), curses.KEY_DOWN):
            self.scroll_active_pane(1)
        elif ch in (ord("k"), curses.KEY_UP):
            self.scroll_active_pane(-1)
        elif ch in (curses.KEY_NPAGE, 4):  # PgDn / Ctrl-D
            pane = getattr(self, "focus_pane", "list")
            bh = getattr(self, "last_drawer_body_h", 8) if pane == "drawer" else getattr(self, "last_list_body_h", 12)
            self.scroll_active_pane(max(1, bh - 2))
        elif ch in (curses.KEY_PPAGE, 21):  # PgUp / Ctrl-U
            pane = getattr(self, "focus_pane", "list")
            bh = getattr(self, "last_drawer_body_h", 8) if pane == "drawer" else getattr(self, "last_list_body_h", 12)
            self.scroll_active_pane(-max(1, bh - 2))
        elif ch in (ord("g"), curses.KEY_HOME):
            if getattr(self, "focus_pane", "list") == "drawer":
                self.drawer_sel = 0
                self.drawer_scroll = 0
            else:
                self.selected_idx = 0
        elif ch in (ord("G"), curses.KEY_END):
            if getattr(self, "focus_pane", "list") == "drawer":
                total = self._drawer_content_len()
                if getattr(self, "right_tab", "upnext") == "upnext":
                    self.drawer_sel = max(0, total - 1)
                else:
                    body_h = max(1, getattr(self, "last_drawer_body_h", 8) - 1)
                    self.drawer_scroll = max(0, total - body_h)
            else:
                self.selected_idx = max(0, len(self.tracks) - 1)
        elif ch == ord("J") and self.active_tab == "queue":
            self.move_queue_selected(1)
        elif ch == ord("K") and self.active_tab == "queue":
            self.move_queue_selected(-1)
        elif ch in (10, 13, curses.KEY_ENTER):
            if getattr(self, "focus_pane", "list") == "drawer" and getattr(self, "right_tab", "upnext") == "upnext":
                self.activate_drawer_item()
            else:
                self.play_selected()
        elif ch == ord("a"):
            self.enqueue_selected()
        elif ch == ord("f"):
            self.handle_action("fav_sel", None)
        elif ch == ord("r"):
            self.handle_action("radio_sel", None)
        elif ch == ord("d"):
            self.handle_action("dl_sel", None)
        elif ch == ord("P"):
            self.handle_action("play_all", None)
        elif ch == ord("E"):
            self.handle_action("add_all", None)
        elif ch == ord("W"):
            self.handle_action("dl_all", None)
        elif ch == ord("X"):
            self.handle_action("dl_cancel", None)
        elif ch == ord("v"):
            self.handle_action("cycle_layout", None)
        elif ch == ord("z"):
            self.handle_action("toggle_drawer", None)
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
        elif ch == ord("A"):
            self.handle_action("autoplay", None)
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
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.pop_nav()
        elif ch == ord("C"):
            self.handle_action("cycle_catalog", None)
        elif ch == ord("T"):
            self.handle_action("cycle_search_type", None)
        elif ch == ord("L"):
            self.handle_action("cycle_local_group", None)
        elif ch == ord("S") and (self.active_tab == "playlists" or getattr(self, "drill_kind", "") == "playlist"):
            self.handle_action("sync_pl", None)
        elif ch == ord("U"):
            self.handle_action("rtab", "upnext")
        elif ch == ord("Y"):
            self.handle_action("rtab", "lyrics")
        elif ch == ord("I"):
            self.handle_action("rtab", "info")
        elif ch == ord("D"):
            self.handle_action("rtab", "downloads")
        elif ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6"), ord("7")):
            idx = ch - ord("1")
            if getattr(self, "focus_pane", "list") == "drawer" and idx < len(self.RIGHT_TABS):
                self.handle_action("rtab", self.RIGHT_TABS[idx][0])
            else:
                self.focus_pane = "list"
                self.handle_action("tab", self.TABS[idx][0])
        elif ch == 9:  # Tab cycles right sub-tabs
            cur_r = next((i for i, (k, _) in enumerate(self.RIGHT_TABS) if k == self.right_tab), 0)
            self.handle_action("rtab", self.RIGHT_TABS[(cur_r + 1) % len(self.RIGHT_TABS)][0])
        elif ch == getattr(curses, "KEY_BTAB", 353):
            cur_r = next((i for i, (k, _) in enumerate(self.RIGHT_TABS) if k == self.right_tab), 0)
            self.handle_action("rtab", self.RIGHT_TABS[(cur_r - 1) % len(self.RIGHT_TABS)][0])
        return True

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
                dr = getattr(self, "last_layout", {}).get("drawer")
                in_drawer = bool(dr and dr[2] <= mx < dr[2] + dr[3] and dr[0] <= my < dr[0] + dr[1])
                if bstate & curses.BUTTON4_PRESSED:
                    self.scroll_active_pane(-3, pane="drawer" if in_drawer else "list")
                    continue
                if bstate & curses.BUTTON5_PRESSED:
                    self.scroll_active_pane(3, pane="drawer" if in_drawer else "list")
                    continue
                for hb in reversed(self.hitboxes):
                    if hb.contains(mx, my):
                        self.handle_action(hb.action, hb.payload, bstate)
                        break
                continue

            if not self.handle_key(wch):
                break


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    initial_query = " ".join(sys.argv[1:]).strip()
    curses.wrapper(lambda stdscr: SovereignDashboard(stdscr, initial_query).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
