#!/usr/bin/env python3
"""Pure layout, formatting, and widget math helpers for musicctl TUI."""

from __future__ import annotations

import curses
import dataclasses
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from common import VIDEO_ID_RE, format_mmss, normalize_track_title, parse_meta_fields

SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
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


def compute_rmpc_layout(
    h: int,
    w: int,
    mode: str = "auto",
    show_drawer: bool = True,
    search_bar: bool = False,
    active_tab: str = "queue",
) -> dict[str, Any]:
    """Compute rmpc-style layout rectangles (y0, h, x0, w) with strict bounds clamping."""
    compact = h < 18
    if not compact:
        header_h = 4
        tab_h = 2
        footer_h = 3
        if w >= 80:
            left_w = min(32, max(22, int(w * 0.24)))
            right_w = min(38, max(24, int(w * 0.32)))
            if w - left_w - right_w < 20:
                right_w = max(22, w - left_w - 20)
                if w - left_w - right_w < 20:
                    left_w = max(20, w - right_w - 20)
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
    hint_rect: tuple[int, int, int, int] | None = None
    if not compact and ch >= 7:
        hint_rect = (fy - 1, 1, 0, w)
        ch -= 1

    content_rect = (cy, ch, 0, w)
    req_mode = mode if mode in LAYOUT_MODES else "auto"
    art_rect: tuple[int, int, int, int] | None = None
    lyrics_rect: tuple[int, int, int, int] | None = None
    drawer_rect: tuple[int, int, int, int] | None = None

    if req_mode == "focus" or not show_drawer:
        effective = "focus"
        list_rect = (cy, ch, 0, w)
    elif active_tab == "queue" and req_mode in ("auto", "split") and w >= 88:
        effective = "split"
        side_w = max(28, int(w * 0.35))
        table_w = w - side_w
        if ch >= 10:
            art_h = ch // 2
            art_rect = (cy, art_h, 0, side_w)
            lyrics_rect = (cy + art_h, ch - art_h, 0, side_w)
        else:
            lyrics_rect = (cy, ch, 0, side_w)
        list_rect = (cy, ch, side_w, table_w)
    elif active_tab != "queue" and req_mode == "auto":
        effective = "focus"
        list_rect = (cy, ch, 0, w)
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
        "art": art_rect,
        "lyrics": lyrics_rect,
        "list": list_rect,
        "drawer": drawer_rect,
        "hint_bar": hint_rect,
        "now_playing": None,
        "footer": footer_rect,
        "footer_mode": footer_mode,
        "footer_progress": footer_progress,
    }


def parse_track_columns(title: str, meta: str, tag: str = "") -> tuple[str, str, str, str]:
    """Extract (artist, clean_title, album, duration) from row fields."""
    clean_title = (title or "").strip()
    fields = parse_meta_fields(meta)
    artist = fields["artist"]
    album = fields["album"]
    duration = fields["duration_str"]

    if not artist and " - " in clean_title:
        left, right = clean_title.split(" - ", 1)
        if left.strip() and right.strip():
            artist = left.strip()
            clean_title = right.strip()

    return (artist or "-", clean_title or "-", album or "-", duration or "-")


def compute_table_col_widths(avail_w: int) -> tuple[int, int, int, int] | None:
    """Compute (artist_w, title_w, album_w, dur_w) for 4-column table, or None when narrow."""
    if avail_w < 54:
        return None
    usable = avail_w - 6
    artist_w = max(8, int(usable * 0.20))
    title_w = max(14, int(usable * 0.35))
    album_w = max(10, int(usable * 0.30))
    dur_w = usable - artist_w - title_w - album_w
    if dur_w < 6:
        need = 6 - dur_w
        take_alb = min(need, max(0, album_w - 6))
        album_w -= take_alb
        need -= take_alb
        take_ttl = min(need, max(0, title_w - 10))
        title_w -= take_ttl
        dur_w = usable - artist_w - title_w - album_w
    return (artist_w, title_w, album_w, dur_w)


def _pad_cell_left(text: str, width: int) -> str:
    clipped = truncate_width(text, width)
    return clipped + " " * max(0, width - display_width(clipped))


def _pad_cell_right(text: str, width: int) -> str:
    clipped = truncate_width(text, width)
    return " " * max(0, width - display_width(clipped)) + clipped


def format_table_header(avail_w: int) -> str:
    """Format aligned 4-column table header row."""
    cols = compute_table_col_widths(avail_w)
    if cols is None:
        return truncate_width("Track", avail_w)
    aw, tw, bw, dw = cols
    return "  ".join(
        [
            _pad_cell_left("Artist", aw),
            _pad_cell_left("Title", tw),
            _pad_cell_left("Album", bw),
            _pad_cell_right("Duration", dw),
        ]
    )


def format_table_row_4col(artist: str, title: str, album: str, duration: str, avail_w: int) -> str:
    """Format track fields into 4 aligned columns or compact fallback when narrow."""
    cols = compute_table_col_widths(avail_w)
    if cols is None:
        compact_meta = " • ".join(p for p in (artist, album, duration) if p and p != "-")
        return format_track_row_columns(title, compact_meta, avail_w)
    aw, tw, bw, dw = cols
    return "  ".join(
        [
            _pad_cell_left(artist, aw),
            _pad_cell_left(title, tw),
            _pad_cell_left(album, bw),
            _pad_cell_right(duration, dw),
        ]
    )


def build_vertical_scrollbar(total: int, visible: int, offset: int, height: int) -> list[str]:
    """Build rmpc-style vertical scrollbar glyph column using │, █, ▲, ▼."""
    if height <= 0:
        return []
    if total <= visible or total <= 0:
        return ["│"] * height
    if height == 1:
        return ["█"]
    if height == 2:
        return ["▲", "▼"]
    track_h = height - 2
    thumb_h = max(1, min(track_h, int(round((visible / float(total)) * track_h))))
    max_off = max(1, total - visible)
    clamped_off = max(0, min(max_off, offset))
    max_top = track_h - thumb_h
    thumb_top = int(round((clamped_off / float(max_off)) * max_top)) if max_top > 0 else 0
    cells = ["│"] * track_h
    for i in range(thumb_top, min(track_h, thumb_top + thumb_h)):
        cells[i] = "█"
    return ["▲", *cells, "▼"]


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


def format_header_title(raw_title: str, artist: str = "") -> str:
    """Normalize header title by removing noise tags and redundant leading artist prefix."""
    cleaned = normalize_track_title(raw_title)
    if not cleaned:
        return ""
    art = (artist or "").strip()
    if art:
        m = re.match(rf"^{re.escape(art)}\s*[-–—]\s+(.+)$", cleaned, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return cleaned


def extract_album_from_meta(meta: str) -> str:
    """Extract album name from bullet-delimited track metadata string."""
    return parse_meta_fields(meta)["album"]


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
    if key in (9, "\t"):
        return query, "cycle_scope"
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


def merge_fav_and_history(
    fav_rows: list[tuple[str, str, str, str]],
    hist_rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Merge Favorites and History TSV rows deduplicated by track id, keeping favorites first."""
    seen: set[str] = set()
    merged: list[tuple[str, str, str, str]] = []
    for row in fav_rows + hist_rows:
        rid = row[0] if row else ""
        if rid and rid not in seen:
            seen.add(rid)
            merged.append(row)
    return merged


def _collect_entity_source_tracks(
    local_entries: list[dict[str, Any]],
    fav_rows: list[tuple[str, str, str, str]],
    hist_rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, tuple[str, str, str, str]]]:
    """Normalize local library, favorites, and history into (artist, album, title_key, row) tuples."""
    items: list[tuple[str, str, str, tuple[str, str, str, str]]] = []
    seen_keys: set[tuple[str, str]] = set()

    for e in local_entries:
        path = str(e.get("path") or "").strip()
        if not path:
            continue
        artist = str(e.get("artist") or "").strip()
        album = str(e.get("album") or "").strip()
        title = str(e.get("title") or e.get("filename") or path).strip()
        dur = int(e.get("duration") or 0)
        dur_lbl = format_mmss(dur) if dur > 0 else str(e.get("codec") or "").strip()
        meta = " • ".join(p for p in (artist, album, dur_lbl) if p) or "Local Audio"
        dedup_k = (path, title.lower())
        if dedup_k in seen_keys:
            continue
        seen_keys.add(dedup_k)
        items.append((artist, album, title.lower(), (path, title, meta, "[LOC]")))

    for vid, title, meta, tag in fav_rows + hist_rows:
        rid = (vid or "").strip()
        if not rid:
            continue
        art_p, ttl_p, alb_p, _dur = parse_track_columns(title, meta, tag)
        artist = "" if art_p == "-" else art_p
        album = "" if alb_p == "-" else alb_p
        dedup_k = (rid, ttl_p.lower())
        if dedup_k in seen_keys:
            continue
        seen_keys.add(dedup_k)
        items.append((artist, album, ttl_p.lower(), (rid, title, meta, tag)))

    return items


def aggregate_artists(
    local_entries: list[dict[str, Any]],
    fav_rows: list[tuple[str, str, str, str]],
    hist_rows: list[tuple[str, str, str, str]],
) -> tuple[list[tuple[str, str, str, str]], dict[str, list[tuple[str, str, str, str]]]]:
    """Aggregate merged local/favorites/history tracks grouped by artist."""
    buckets: dict[str, list[tuple[str, str, str, str]]] = {}
    display_names: dict[str, str] = {}
    for artist, _album, _tkey, row in _collect_entity_source_tracks(local_entries, fav_rows, hist_rows):
        disp = artist or "Unknown Artist"
        norm_k = disp.lower()
        display_names.setdefault(norm_k, disp)
        buckets.setdefault(norm_k, []).append(row)

    summary_rows: list[tuple[str, str, str, str]] = []
    groups_map: dict[str, list[tuple[str, str, str, str]]] = {}
    for norm_k in sorted(buckets.keys(), key=lambda k: (1 if display_names[k] == "Unknown Artist" else 0, k)):
        disp = display_names[norm_k]
        tracks = buckets[norm_k]
        key = f"eart:{disp}"
        groups_map[key] = tracks
        summary_rows.append((key, disp, f"{len(tracks)} tracks", "[E-ART]"))
    return summary_rows, groups_map


def aggregate_albums(
    local_entries: list[dict[str, Any]],
    fav_rows: list[tuple[str, str, str, str]],
    hist_rows: list[tuple[str, str, str, str]],
) -> tuple[list[tuple[str, str, str, str]], dict[str, list[tuple[str, str, str, str]]]]:
    """Aggregate merged local/favorites/history tracks grouped by album."""
    buckets: dict[str, list[tuple[str, tuple[str, str, str, str]]]] = {}
    display_names: dict[str, str] = {}
    for artist, album, _tkey, row in _collect_entity_source_tracks(local_entries, fav_rows, hist_rows):
        disp = album or "Unknown Album"
        norm_k = disp.lower()
        display_names.setdefault(norm_k, disp)
        buckets.setdefault(norm_k, []).append((artist, row))

    summary_rows: list[tuple[str, str, str, str]] = []
    groups_map: dict[str, list[tuple[str, str, str, str]]] = {}
    for norm_k in sorted(buckets.keys(), key=lambda k: (1 if display_names[k] == "Unknown Album" else 0, k)):
        disp = display_names[norm_k]
        pairs = buckets[norm_k]
        tracks = [r for _a, r in pairs]
        artists = {a for a, _r in pairs if a}
        if len(artists) == 1:
            artist_lbl = next(iter(artists))
        elif len(artists) > 1:
            artist_lbl = "Various Artists"
        else:
            artist_lbl = "Unknown Artist"
        key = f"ealb:{disp}"
        groups_map[key] = tracks
        summary_rows.append((key, disp, f"{artist_lbl} • {len(tracks)} tracks", "[E-ALB]"))
    return summary_rows, groups_map


CATALOG_CONTAINER_RE = re.compile(r"^(?:ytm|deezer|itunes):(?:album|artist|playlist):")
CATALOG_TRACK_RE = re.compile(r"^(?:ytm|deezer|itunes):(?:song|track):")
GROUP_LIST_TAGS = frozenset({"[L-ART]", "[L-ALB]", "[E-ART]", "[E-ALB]", "[PL]", "[ART]", "[ALB]"})


_HINT_ACTIONS: dict[tuple[str, str], tuple[str, Any]] = {
    ("Enter", "Search"): ("do_search", None),
    ("Esc", "Done"): ("blur_search", None),
    ("Ctrl-U", "Clear"): ("clear_search", None),
    ("Tab", "Scope"): ("cycle_search_type", None),
    ("X", "Cancel All"): ("dl_cancel", None),
    ("Esc", "Close"): ("close_modal", None),
    ("Space", "Pause"): ("toggle", None),
    ("Enter", "Play"): ("play_sel", None),
    ("Enter", "Open"): ("play_sel", None),
    ("Enter", "Jump"): ("play_sel", None),
    ("a", "+Queue"): ("add_sel", None),
    ("a", "+Queue All"): ("add_sel", None),
    ("f", "Fav"): ("fav_sel", None),
    ("d", "DL"): ("dl_sel", None),
    ("d", "Download"): ("dl_sel", None),
    ("r", "Radio"): ("radio_sel", None),
    ("I", "Info"): ("rtab", "info"),
    ("D", "DL Modal"): ("rtab", "downloads"),
    ("/", "Search"): ("focus_search", None),
    ("Bksp", "Back"): ("nav_back", None),
    ("?", "Help"): ("help", None),
}


def hint_action_for_key(k_txt: str, lbl: str) -> tuple[str, Any] | None:
    """Map a bottom context-hint (key, label) pair to a dashboard action tuple."""
    return _HINT_ACTIONS.get((k_txt, lbl))


def classify_row(vid: str, tag: str = "") -> str:
    """Classify a dashboard list row into a canonical routing kind."""
    v = (vid or "").strip()
    t = (tag or "").strip()
    if t in ("[E-ART]", "[E-ALB]") or v.startswith(("eart:", "ealb:")):
        return "entity_group"
    if t in ("[L-ART]", "[L-ALB]") or v.startswith(("lart:", "lalb:")):
        return "local_group"
    if v.startswith("pl:"):
        return "playlist"
    if t in ("[ALB]", "[ART]", "[PL]") or bool(CATALOG_CONTAINER_RE.match(v)):
        return "catalog_container"
    if CATALOG_TRACK_RE.match(v):
        return "catalog_track"
    if t == "[Q]":
        return "queue_item"
    if t == "[LOC]" or v.startswith(("/", "~")):
        return "local_file"
    return "track"


def is_downloadable_ref(vid: str, tag: str = "") -> bool:
    """Return True when row references a downloadable remote/playlist target."""
    v = (vid or "").strip()
    if not v:
        return False
    return classify_row(v, tag) not in ("entity_group", "local_group", "local_file")


def calc_queue_art_size(art_rect: tuple[int, int, int, int] | None) -> tuple[int, int] | None:
    """Compute (tw, th) for the WP4 Queue left Album Art pane, or None when hidden/too small."""
    if art_rect is None:
        return None
    in_h = max(0, art_rect[1] - 1)
    in_w = max(0, art_rect[3] - 4)
    if in_h < 4 or in_w < 12:
        return None
    th = min(14, in_h)
    tw = min(in_w, max(12, th * 2 + 2))
    return (tw, th)


def build_context_hints(
    active_tab: str,
    *,
    search_focus: bool = False,
    active_modal: str = "",
    has_nav: bool = False,
    is_group_list: bool = False,
) -> list[tuple[str, str]]:
    """Return context-sensitive (key, label) hints for the bottom action strip."""
    if search_focus:
        return [("Enter", "Search"), ("Esc", "Done"), ("Ctrl-U", "Clear"), ("Tab", "Scope")]
    if active_modal == "downloads":
        return [("j/k", "Scroll"), ("X", "Cancel All"), ("Esc", "Close")]
    if active_modal == "info":
        return [("Enter", "Play"), ("a", "+Queue"), ("f", "Fav"), ("d", "DL"), ("r", "Radio"), ("Esc", "Close")]
    if active_tab == "lyrics" and not has_nav:
        return [("j/k", "Scroll"), ("Space", "Pause"), ("</>", "Prev/Next"), (",/.", "Vol"), ("?", "Help")]
    if is_group_list:
        return [("Enter", "Open"), ("a", "+Queue All"), ("d", "Download"), ("/", "Search"), ("Space", "Pause"), ("?", "Help")]
    if active_tab == "queue" and not has_nav:
        return [("Enter", "Jump"), ("J/K", "Move"), ("f", "Fav"), ("d", "DL"), ("r", "Radio"), ("I", "Info"), ("D", "DL Modal"), ("?", "Help")]
    back_or_search = ("Bksp", "Back") if has_nav else ("/", "Search")
    return [("Enter", "Play"), ("a", "+Queue"), ("f", "Fav"), ("d", "DL"), ("r", "Radio"), ("I", "Info"), back_or_search, ("?", "Help")]
