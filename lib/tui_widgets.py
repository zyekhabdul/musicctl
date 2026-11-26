#!/usr/bin/env python3
"""Pure layout, formatting, and widget math helpers for musicctl TUI."""

from __future__ import annotations

import curses
import dataclasses
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from common import VIDEO_ID_RE, format_mmss

SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
WAVE_BARS = "▁▂▃▄▅▆▇█"
LAYOUT_MODES = ("auto", "split", "stacked", "focus")

RMPC_TABS = [
    ("queue", "1 Queue"),
    ("artists", "2 Artists"),
    ("albums", "3 Albums"),
    ("playlists", "4 Playlists"),
    ("local", "5 Local"),
    ("search", "6 Search"),
    ("hist", "7 History"),
    ("lyrics", "8 Lyrics"),
]


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


def compute_rmpc_layout(
    h: int,
    w: int,
    mode: str = "auto",
    show_drawer: bool = True,
    search_bar: bool = False,
) -> dict[str, Any]:
    """Compute rmpc-style layout rectangles (y0, h, x0, w) with strict bounds clamping."""
    compact = h < 18
    if not compact:
        header_h = 4
        tab_h = 2
        footer_h = 3
        if w >= 80:
            left_w = min(35, max(22, w // 4))
            right_w = min(35, max(22, w // 4))
            center_w = w - left_w - right_w
            header_boxes: dict[str, tuple[int, int, int, int] | None] = {
                "left": (0, header_h, 0, left_w),
                "center": (0, header_h, left_w, center_w),
                "right": (0, header_h, left_w + center_w, right_w),
            }
        else:
            right_w = min(26, max(18, w // 3))
            left_w = w - right_w
            header_boxes = {
                "left": (0, header_h, 0, left_w),
                "center": None,
                "right": (0, header_h, left_w, right_w),
            }
        tabs_rect = (header_h, tab_h, 0, w)
        mode_w = 12 if w >= 56 else 10
        fy = h - footer_h
        footer_rect = (fy, footer_h, 0, w)
        footer_mode = (fy, footer_h, 0, mode_w)
        footer_progress = (fy, footer_h, mode_w, w - mode_w)
    else:
        header_h = 1
        tab_h = 1
        footer_h = 2
        header_boxes = {
            "left": (0, header_h, 0, w),
            "center": None,
            "right": None,
        }
        tabs_rect = (header_h, tab_h, 0, w)
        mode_w = 10
        fy = h - footer_h
        footer_rect = (fy, footer_h, 0, w)
        footer_mode = (fy, footer_h, 0, mode_w)
        footer_progress = (fy, footer_h, mode_w, w - mode_w)

    cy = header_h + tab_h
    ch = max(4, h - header_h - tab_h - footer_h)
    search_rect: tuple[int, int, int, int] | None = None
    if search_bar and ch >= 6:
        search_rect = (cy, 1, 0, w)
        cy += 1
        ch -= 1

    content_rect = (cy, ch, 0, w)
    req_mode = mode if mode in LAYOUT_MODES else "auto"
    if req_mode == "focus" or not show_drawer:
        effective = "focus"
        list_rect = (cy, ch, 0, w)
        drawer_rect: tuple[int, int, int, int] | None = None
    elif (req_mode == "split" or (req_mode == "auto" and w >= 96)) and w >= 64:
        effective = "split"
        left_w = max(36, min(w - 28, int(w * 0.62)))
        right_w = w - left_w
        list_rect = (cy, ch, 0, left_w)
        drawer_rect = (cy, ch, left_w, right_w)
    else:
        effective = "stacked"
        if ch < 10:
            list_rect = (cy, ch, 0, w)
            drawer_rect = None
        else:
            dr_h = max(4, min(14, int(ch * 0.34)))
            list_h = ch - dr_h
            list_rect = (cy, list_h, 0, w)
            drawer_rect = (cy + list_h, dr_h, 0, w)

    return {
        "compact": compact,
        "effective": effective,
        "header": (0, header_h, 0, w),
        "header_boxes": header_boxes,
        "tabs": tabs_rect,
        "search_bar": search_rect,
        "content": content_rect,
        "list": list_rect,
        "drawer": drawer_rect,
        "now_playing": None,
        "footer": footer_rect,
        "footer_mode": footer_mode,
        "footer_progress": footer_progress,
    }


def format_queue_summary(playlist: list[dict[str, Any]], cur_duration: float = 0.0) -> str:
    """Format 'N songs / MM:SS total time' summary for the footer border title."""
    n = len(playlist)
    total = 0.0
    for entry in playlist:
        if not isinstance(entry, dict):
            continue
        try:
            d = float(entry.get("duration") or 0.0)
        except (TypeError, ValueError):
            d = 0.0
        if d > 0:
            total += d
    if total <= 0 and n == 1 and cur_duration > 0:
        total = float(cur_duration)
    unit = "song" if n == 1 else "songs"
    if total > 0:
        return f"{n} {unit} / {format_mmss(total)} total time"
    return f"{n} {unit}"


def build_volume_slider(vol: int, bar_w: int) -> tuple[str, str, str]:
    """Build rmpc-style volume slider parts (left_track, thumb, right_track)."""
    if bar_w <= 0:
        return ("", "", "")
    if bar_w == 1:
        return ("", "●", "")
    clamped = max(0, min(100, int(vol)))
    pos = int(round((clamped / 100.0) * (bar_w - 1)))
    return ("─" * pos, "●", "─" * (bar_w - 1 - pos))


def build_progress_bar(pos: float, dur: float, bar_w: int) -> tuple[str, str]:
    """Build rmpc-style progress bar parts (filled, empty)."""
    if bar_w <= 0:
        return ("", "")
    if dur <= 0:
        return ("", "─" * bar_w)
    ratio = max(0.0, min(1.0, float(pos) / float(dur)))
    fill_w = max(0, min(bar_w, int(round(ratio * bar_w))))
    return ("█" * fill_w, " " * (bar_w - fill_w))


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
