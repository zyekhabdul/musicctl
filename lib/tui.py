#!/usr/bin/env python3
"""Native clickable & realtime curses Sovereign Dashboard for musicctl."""

from __future__ import annotations

import curses
import json
import locale
import os
import queue
import re
import subprocess
import sys
import threading
import time
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
    parse_meta_fields,
    parse_synced_lyrics_lines,
    pick_best_lyrics_candidate,
)
from tui_widgets import (
    GROUP_LIST_TAGS,
    LAYOUT_MODES,
    RMPC_TABS,
    SGR_RE,
    Hitbox,
    aggregate_albums,
    aggregate_artists,
    apply_search_key,
    build_context_hints,
    build_progress_bar,
    build_vertical_scrollbar,
    build_volume_slider,
    calc_queue_art_size,
    calc_seek_target,
    calc_vol_target,
    classify_row,
    compute_rmpc_layout,
    compute_table_col_widths,
    display_width,
    enrich_tsv_entry,
    extract_album_from_meta,
    format_album_line,
    format_audio_badge,
    format_header_title,
    format_queue_summary,
    format_table_header,
    format_table_row_4col,
    format_track_row_columns,
    is_downloadable_ref,
    is_placeholder_title,
    merge_fav_and_history,
    parse_ansi_art,
    parse_download_jsonl_line,
    parse_track_columns,
    truncate_width,
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


class SovereignDashboard:
    """Interactive clickable & realtime curses dashboard for musicctl."""

    TABS = list(RMPC_TABS)
    RIGHT_TABS = [("upnext", "U Up Next"), ("lyrics", "Y Lyrics"), ("info", "I Info"), ("downloads", "D DL")]
    CATALOG_SOURCES = ("ytm", "deezer", "itunes")
    SEARCH_TYPES = ("songs", "albums", "artists", "playlists")
    LOCAL_GROUP_MODES = ("flat", "artist", "album")

    def __init__(
        self,
        stdscr: Any = None,
        initial_query: str = "",
        *,
        bin_path: str | None = None,
        sock_path: str | None = None,
        data_dir: Path | None = None,
        cache_dir: Path | None = None,
        download_dir: Path | None = None,
        art_enabled: bool | None = None,
        mpv: Any = None,
        dl_manager: Any = None,
    ) -> None:
        self.stdscr = stdscr
        here = Path(__file__).resolve()
        fallback_bin = next(
            (
                str(p)
                for p in (
                    here.parent.parent / "bin" / "musicctl",
                    here.parent.parent.parent / "bin" / "musicctl",
                )
                if p.is_file()
            ),
            str(here.parent.parent / "bin" / "musicctl"),
        )
        self.bin_path = bin_path or os.environ.get("MUSICCTL_BIN") or fallback_bin
        self.sock_path = sock_path or os.environ.get("MUSICCTL_SOCKET") or os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "musicctl.sock")
        self.data_dir = Path(data_dir) if data_dir is not None else Path(os.environ.get("MUSICCTL_DATA_DIR") or os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "musicctl"))
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path(os.environ.get("MUSICCTL_CACHE_DIR") or os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "musicctl"))
        self.download_dir = Path(download_dir) if download_dir is not None else Path(os.environ.get("MUSICCTL_DOWNLOAD_DIR") or os.path.expanduser("~/Music/musicctl"))
        self.art_enabled = (os.environ.get("MUSICCTL_ART", "1") == "1") if art_enabled is None else bool(art_enabled)

        self.mpv = mpv if mpv is not None else MpvClient(self.sock_path)
        self.lock = threading.Lock()
        self.ui_events: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()

        self.active_tab = "search" if initial_query else "queue"
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
        self.active_modal = ""

        cat_env = (os.environ.get("MUSICCTL_CATALOG") or "ytm").strip().lower()
        self.catalog_source = cat_env if cat_env in self.CATALOG_SOURCES else "ytm"
        self.search_type = "songs"
        self.local_group_mode = "flat"
        self.nav_stack: list[dict[str, Any]] = []
        self.drill_title = ""
        self.drill_ref = ""
        self.drill_kind = ""
        self._local_groups: dict[str, list[dict[str, Any]]] = {}
        self._entity_groups: dict[str, list[tuple[str, str, str, str]]] = {}
        self._row_links: dict[str, dict[str, tuple[str, str]]] = {}
        self._cached_favs: list[tuple[str, str, str, str]] = []
        self._cached_hist: list[tuple[str, str, str, str]] = []
        self._track_meta_index: dict[str, tuple[str, str]] = {}

        self.tracks: list[tuple[str, str, str, str]] = []
        self.search_results: list[tuple[str, str, str, str]] = []
        self.selected_idx = 0
        self.scroll_top = 0

        self.dl_manager = dl_manager if dl_manager is not None else DownloadManager(self.bin_path, self.ui_events)
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
        self._last_queue_sig: tuple[Any, ...] | None = None

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
            curses.init_pair(PAIR_BAR_EMPTY, 243, -1)
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

    def _rebuild_track_meta_index(self) -> None:
        idx: dict[str, tuple[str, str]] = {}
        for rid, rtitle, rmeta, _tag in [*getattr(self, "_cached_hist", []), *getattr(self, "_cached_favs", [])]:
            key = (rid or "").strip()
            ttl = (rtitle or "").strip()
            if not key or not ttl or is_placeholder_title(ttl, key):
                continue
            meta_s = (rmeta or "").strip()
            rich = bool(meta_s and meta_s not in ("Video ID", "Direct URL"))
            for k in {key, extract_video_id(key)}:
                if not k:
                    continue
                prev = idx.get(k)
                if prev is None or (rich and not (prev[1] and prev[1] not in ("Video ID", "Direct URL"))):
                    idx[k] = (ttl, meta_s)
        self._track_meta_index = idx

    def refresh_local_files_meta(self) -> None:
        sigs = getattr(self, "_file_sigs", None)
        if sigs is None:
            sigs = {}
            self._file_sigs = sigs

        meta_dirty = False
        fav_path = self.data_dir / "favorites.tsv"
        fav_sig = self._file_sig(fav_path)
        if fav_sig != sigs.get("fav", ...):
            sigs["fav"] = fav_sig
            favs = self.read_tsv(fav_path, "[FAV]") if fav_sig is not None else []
            self._cached_favs = favs
            self.fav_ids = {x[0] for x in favs}
            meta_dirty = True

        hist_path = self.data_dir / "history.tsv"
        hist_sig = self._file_sig(hist_path)
        if hist_sig != sigs.get("hist", ...):
            sigs["hist"] = hist_sig
            self._cached_hist = self.read_tsv(hist_path, "[HIS]") if hist_sig is not None else []
            meta_dirty = True

        if meta_dirty:
            self._rebuild_track_meta_index()

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

    def load_local_grouped(self, mode: str | None = None) -> list[tuple[str, str, str, str]]:
        cache_file = self.cache_dir / "library.json"
        entries = library.scan_library(self.download_dir, cache_file)
        eff_mode = mode if mode in self.LOCAL_GROUP_MODES else getattr(self, "local_group_mode", "flat")
        groups_map: dict[str, list[dict[str, Any]]] = {}
        self._local_groups = groups_map

        if eff_mode == "artist":
            rows: list[tuple[str, str, str, str]] = []
            for g in library.group_by_artist(entries):
                key = f"lart:{g['artist']}"
                groups_map[key] = g["tracks"]
                rows.append((key, g["artist"], f"{len(g['tracks'])} tracks", "[L-ART]"))
            return rows

        if eff_mode == "album":
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

    def _queue_state_sig(self) -> tuple[Any, ...]:
        pl = self.mpv_state.get("playlist") or []
        pl_sig = tuple(
            (e.get("filename") or "", e.get("title") or "", bool(e.get("current")))
            for e in pl
            if isinstance(e, dict)
        )
        mf = extract_metadata_fields(self.mpv_state.get("metadata"))
        dur = int(round(float(self.mpv_state.get("duration") or 0.0)))
        hist_sig = self._file_sig(self.data_dir / "history.tsv")
        fav_sig = self._file_sig(self.data_dir / "favorites.tsv")
        return (pl_sig, mf.get("artist") or "", mf.get("album") or "", dur, hist_sig, fav_sig)

    def _resolve_queue_entry(
        self,
        entry: dict[str, Any],
        idx: int,
        cur_mf: dict[str, Any] | None = None,
        cur_dur: float = 0.0,
    ) -> tuple[str, str, str]:
        fname = str(entry.get("filename") or "")
        raw_title = str(entry.get("title") or "").strip()
        vid = extract_video_id(fname)
        is_cur = bool(entry.get("current"))
        meta_idx = getattr(self, "_track_meta_index", {})
        known = meta_idx.get(vid) or meta_idx.get(fname)
        k_title, k_meta = known if known else ("", "")
        k_fields = (
            parse_meta_fields(k_meta)
            if k_meta
            else {"artist": "", "album": "", "duration": 0, "duration_str": ""}
        )

        if is_cur and cur_mf:
            artist = cur_mf.get("artist") or k_fields.get("artist") or ""
            album = cur_mf.get("album") or k_fields.get("album") or ""
            dur_s = format_mmss(cur_dur) if cur_dur > 0 else (k_fields.get("duration_str") or "")
        else:
            artist = k_fields.get("artist") or ""
            album = k_fields.get("album") or ""
            dur_s = k_fields.get("duration_str") or ""
            if not dur_s:
                try:
                    dval = float(entry.get("duration") or 0.0)
                except (TypeError, ValueError):
                    dval = 0.0
                if dval > 0:
                    dur_s = format_mmss(dval)

        if raw_title and not is_placeholder_title(raw_title, vid):
            title = format_header_title(raw_title, artist) or raw_title
        elif k_title and not is_placeholder_title(k_title, vid):
            title = format_header_title(k_title, artist) or k_title
        elif fname.startswith("/"):
            title = Path(fname).stem
        else:
            title = raw_title or fname or "Unknown"

        parts = [p for p in (artist, album, dur_s) if p]
        if not parts:
            parts = [f"#{idx + 1}", "Active" if is_cur else "Queued"]
        return (vid, title, " • ".join(parts))

    def load_tab_items(self) -> None:
        self.refresh_local_files_meta()
        if getattr(self, "nav_stack", None):
            if self.selected_idx >= len(self.tracks):
                self.selected_idx = max(0, len(self.tracks) - 1)
            if self.scroll_top > self.selected_idx:
                self.scroll_top = self.selected_idx
            return

        if self.active_tab in ("home", "hist"):
            favs = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
            hist = self.read_tsv(self.data_dir / "history.tsv", "[HIS]")
            self.tracks = merge_fav_and_history(favs, hist)
        elif self.active_tab == "search":
            self.tracks = list(self.search_results)
        elif self.active_tab == "fav":
            self.tracks = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
        elif self.active_tab == "queue":
            pl = self.mpv_state.get("playlist") or []
            qitems: list[tuple[str, str, str, str]] = []
            cur_mf = extract_metadata_fields(self.mpv_state.get("metadata"))
            cur_dur = float(self.mpv_state.get("duration") or 0.0)
            for idx, entry in enumerate(pl):
                if not isinstance(entry, dict):
                    continue
                vid, title, meta_s = self._resolve_queue_entry(entry, idx, cur_mf, cur_dur)
                qitems.append((vid, title, meta_s, "[Q]"))
            self.tracks = qitems
            self._last_queue_sig = self._queue_state_sig()
        elif self.active_tab in ("artists", "albums"):
            entries = library.scan_library(self.download_dir, self.cache_dir / "library.json")
            favs = self.read_tsv(self.data_dir / "favorites.tsv", "[FAV]")
            hist = self.read_tsv(self.data_dir / "history.tsv", "[HIS]")
            if self.active_tab == "artists":
                rows, groups = aggregate_artists(entries, favs, hist)
            else:
                rows, groups = aggregate_albums(entries, favs, hist)
            self._entity_groups = groups
            self.tracks = rows
        elif self.active_tab == "local":
            self.tracks = self.load_local_grouped()
        elif self.active_tab == "playlists":
            self.tracks = self.load_playlists_list()
        elif self.active_tab == "lyrics":
            self.right_tab = "lyrics"
            self._ensure_lyrics_for_current()

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
                art_sz = calc_queue_art_size(getattr(self, "last_layout", {}).get("art"))
                if art_sz:
                    self.ensure_art_async(vid, art_sz[0], art_sz[1])
                if self.active_tab == "queue":
                    self.load_tab_items()

            if not idle and cur_path and self.is_lyrics_visible() and self.lyrics_track_key != cur_path and not self.lyrics_loading:
                if is_placeholder_title(title, vid):
                    self.lyrics_header = "Waiting for track metadata..."
                else:
                    self.fetch_lyrics_async(title, cur_path, artist=artist, duration=dur)
        else:
            self.mpv_state = {}

    def is_lyrics_visible(self) -> bool:
        """Return True when lyrics are rendered in the active tab, Queue split, or drawer."""
        if self.active_tab == "lyrics" and not getattr(self, "nav_stack", None):
            return True
        layout = getattr(self, "last_layout", {}) or {}
        if layout.get("lyrics") is not None:
            return True
        if layout.get("drawer") is not None and getattr(self, "right_tab", "upnext") == "lyrics":
            return True
        if not layout and getattr(self, "right_tab", "upnext") == "lyrics":
            return True
        return False

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

    def _draw_header_flags(self, y: int, x0: int, avail_w: int, muted: bool) -> None:
        if avail_w <= 6:
            return
        loop_m = self.get_loop_mode()
        if avail_w >= 34:
            lp_lbl = "Loop:1" if loop_m == "one" else ("Loop:A" if loop_m == "all" else "Loop")
            flags = [
                (lp_lbl, "loop", loop_m != "off"),
                ("Shuf", "shuffle", False),
                ("EQ", "eq", getattr(self, "eq_mode", "flat") != "flat"),
                ("Norm", "norm", bool(getattr(self, "norm_on", False))),
                ("Auto", "autoplay", bool(getattr(self, "autoplay_on", False))),
                ("Mute", "mute", muted),
            ]
        elif avail_w >= 23:
            lp_lbl = "Lp:1" if loop_m == "one" else ("Lp:A" if loop_m == "all" else "Lp")
            flags = [
                (lp_lbl, "loop", loop_m != "off"),
                ("Sh", "shuffle", False),
                ("EQ", "eq", getattr(self, "eq_mode", "flat") != "flat"),
                ("Nm", "norm", bool(getattr(self, "norm_on", False))),
                ("Au", "autoplay", bool(getattr(self, "autoplay_on", False))),
                ("Mt", "mute", muted),
            ]
        else:
            flags = [
                ("o", "loop", loop_m != "off"),
                ("u", "shuffle", False),
                ("e", "eq", getattr(self, "eq_mode", "flat") != "flat"),
                ("N", "norm", bool(getattr(self, "norm_on", False))),
                ("A", "autoplay", bool(getattr(self, "autoplay_on", False))),
                ("m", "mute", muted),
            ]
        help_btn = "[?]"
        fx = x0
        max_fx = x0 + avail_w - len(help_btn) - 1
        for lbl, act, is_act in flags:
            if fx + len(lbl) > max_fx:
                break
            pair = PAIR_RED if (act == "mute" and muted) else (PAIR_AMBER if is_act else PAIR_DIM)
            attr = curses.color_pair(pair) | (curses.A_BOLD if is_act else 0)
            self.safe_addstr(y, fx, lbl, attr)
            self.add_hitbox(fx, y, fx + len(lbl), y + 1, act, None)
            fx += len(lbl) + 1
        hx = max(fx, x0 + avail_w - len(help_btn))
        if hx + len(help_btn) <= x0 + avail_w:
            h_attr = curses.color_pair(PAIR_PILL_ACTIVE if self.show_help else PAIR_CYAN) | curses.A_BOLD
            self.safe_addstr(y, hx, help_btn, h_attr)
            self.add_hitbox(hx, y, hx + len(help_btn), y + 1, "help", None)

    def render_rmpc_header(self, layout: dict[str, Any]) -> None:
        compact = bool(layout.get("compact"))
        boxes = layout["header_boxes"]
        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        paused = bool(self.mpv_state.get("pause", False))
        badge = "[IDLE]" if idle else ("[|| PAUSE]" if paused else "[> PLAY]")
        compact_badge = "[IDLE]" if idle else ("[PAUSE]" if paused else "[PLAY]")
        badge_pair = PAIR_DIM if idle else (PAIR_AMBER if paused else PAIR_GREEN)
        pos_s = format_mmss(float(self.mpv_state.get("time-pos") or 0.0))
        dur_s = format_mmss(float(self.mpv_state.get("duration") or 0.0))
        codec = str(self.mpv_state.get("audio-codec-name") or "")
        srate = self.mpv_state.get("audio-params/samplerate") or 0
        sfmt = str(self.mpv_state.get("audio-params/format") or "")
        audio_badge = format_audio_badge(codec, srate, sfmt)
        spd_lbl = format_speed(self.mpv_state.get("speed"))
        q_lbl = str(getattr(self, "quality_mode", "cd") or "cd").upper()

        mf = extract_metadata_fields(self.mpv_state.get("metadata"))
        artist = mf.get("artist") or ""
        raw_title = str(self.mpv_state.get("media-title") or self.mpv_state.get("filename") or "")
        if idle or not raw_title:
            title = "No Song"
        else:
            title = format_header_title(raw_title, artist) or raw_title
        album = format_album_line(mf.get("album", ""), mf.get("year", 0))
        sub_line = "No active playback" if idle else (" - ".join([p for p in (artist, album) if p]) or "Unknown")
        vol = max(0, min(100, int(round(float(self.mpv_state.get("volume") or 100)))))
        muted = bool(self.mpv_state.get("mute", False))

        if compact:
            _, _, _, hw = layout["header"]
            self.safe_addstr(0, 0, " " * hw, curses.color_pair(PAIR_PILL_BTN))
            self.safe_addstr(0, 1, compact_badge, curses.color_pair(badge_pair) | curses.A_BOLD)
            self.add_hitbox(1, 0, 1 + len(compact_badge), 1, "toggle", None)
            help_btn = "[?]"
            vol_lbl = f"{vol:3d}%"
            hx = max(2 + len(compact_badge), hw - len(help_btn) - 1)
            vx = max(2 + len(compact_badge), hx - len(vol_lbl) - 1)
            title_x = 2 + len(compact_badge)
            title_w = max(0, vx - title_x - 1)
            if title_w > 2:
                summary_t = title if idle or not artist else f"{title} — {artist}"
                self.safe_addstr(0, title_x, truncate_width(summary_t, title_w), curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
            if vx + len(vol_lbl) < hx:
                self.safe_addstr(0, vx, vol_lbl, curses.color_pair(PAIR_RED if muted else PAIR_CYAN))
                self.add_hitbox(vx, 0, vx + len(vol_lbl), 1, "mute", None)
            self.safe_addstr(0, hx, help_btn, curses.color_pair(PAIR_PILL_ACTIVE if self.show_help else PAIR_CYAN) | curses.A_BOLD)
            self.add_hitbox(hx, 0, hx + len(help_btn), 1, "help", None)
            return

        _, _, _, lw = boxes["left"]
        _, _, rx, rw = boxes["right"]
        center = boxes["center"]
        b_attr = curses.color_pair(PAIR_BOX)

        if center is not None:
            _, _, cx, cw = center
            top_line = "╭" + "─" * max(0, lw - 1) + "┬" + "─" * max(0, cw - 1) + "┬" + "─" * max(0, rw - 2) + "╮"
            bot_line = "├" + "─" * max(0, lw - 1) + "┴" + "─" * max(0, cw - 1) + "┴" + "─" * max(0, rw - 2) + "┤"
            self.safe_addstr(0, 0, top_line, b_attr)
            for row_y in (1, 2):
                self.safe_addstr(row_y, 0, "│", b_attr)
                self.safe_addstr(row_y, lw, "│", b_attr)
                self.safe_addstr(row_y, cx + cw, "│", b_attr)
                self.safe_addstr(row_y, rx + rw - 1, "│", b_attr)
            self.safe_addstr(3, 0, bot_line, b_attr)

            l_in_w = max(1, lw - 2)
            tx_pos = 1
            if l_in_w >= 19:
                tx_pos = self.draw_button(1, tx_pos, "<", "prev", None)
                b_txt = truncate_width(badge, max(1, lw - tx_pos - 5))
                self.safe_addstr(1, tx_pos, b_txt, curses.color_pair(badge_pair) | curses.A_BOLD)
                self.add_hitbox(tx_pos, 1, min(lw, tx_pos + len(b_txt)), 2, "toggle", None)
                tx_pos += len(b_txt) + 1
                if tx_pos + 3 < lw:
                    tx_pos = self.draw_button(1, tx_pos, ">", "next", None)
            else:
                b_txt = truncate_width(compact_badge, l_in_w)
                self.safe_addstr(1, 1, b_txt, curses.color_pair(badge_pair) | curses.A_BOLD)
                self.add_hitbox(1, 1, min(lw, 1 + len(b_txt)), 2, "toggle", None)
                tx_pos = 2 + len(b_txt)
            if spd_lbl and tx_pos + len(spd_lbl) <= lw:
                self.safe_addstr(1, tx_pos, spd_lbl, curses.color_pair(PAIR_AMBER))

            if audio_badge:
                l2 = f"{audio_badge} | {q_lbl}"
            elif not idle:
                l2 = f"{pos_s} / {dur_s} | {q_lbl}"
            else:
                l2 = f"Ready | Quality: {q_lbl}"
            self.safe_addstr(2, 1, truncate_width(l2, l_in_w), curses.color_pair(PAIR_CYAN))

            c_in_w = max(1, cw - 2)
            t_disp = truncate_width(title, c_in_w)
            t_pad = max(0, (c_in_w - display_width(t_disp)) // 2)
            self.safe_addstr(1, cx + 1 + t_pad, t_disp, curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
            s_disp = truncate_width(sub_line, c_in_w)
            s_pad = max(0, (c_in_w - display_width(s_disp)) // 2)
            self.safe_addstr(2, cx + 1 + s_pad, s_disp, curses.color_pair(PAIR_DIM if idle else PAIR_AMBER) | (0 if idle else curses.A_BOLD))
        else:
            top_line = "╭" + "─" * max(0, lw - 1) + "┬" + "─" * max(0, rw - 2) + "╮"
            bot_line = "├" + "─" * max(0, lw - 1) + "┴" + "─" * max(0, rw - 2) + "┤"
            self.safe_addstr(0, 0, top_line, b_attr)
            for row_y in (1, 2):
                self.safe_addstr(row_y, 0, "│", b_attr)
                self.safe_addstr(row_y, lw, "│", b_attr)
                self.safe_addstr(row_y, rx + rw - 1, "│", b_attr)
            self.safe_addstr(3, 0, bot_line, b_attr)

            l_in_w = max(1, lw - 2)
            self.safe_addstr(1, 1, truncate_width(compact_badge, l_in_w), curses.color_pair(badge_pair) | curses.A_BOLD)
            self.add_hitbox(1, 1, min(lw, 1 + len(compact_badge)), 2, "toggle", None)
            rem_t = max(0, l_in_w - len(compact_badge) - 1)
            if rem_t > 1:
                self.safe_addstr(1, 2 + len(compact_badge), truncate_width(title, rem_t), curses.color_pair(PAIR_TITLE) | curses.A_BOLD)
            elap = f"{pos_s} / {dur_s}" + (f" ({audio_badge})" if audio_badge else "")
            self.safe_addstr(2, 1, truncate_width(elap, l_in_w), curses.color_pair(PAIR_CYAN))

        r_in_w = max(1, rw - 2)
        vol_prefix = "Vol " if r_in_w >= 16 else ""
        pct_lbl = f" {vol:3d}%"
        sx = rx + 1 + len(vol_prefix)
        slider_w = max(4, r_in_w - len(vol_prefix) - len(pct_lbl))
        if vol_prefix:
            self.safe_addstr(1, rx + 1, vol_prefix, curses.color_pair(PAIR_DIM))
        s_left, s_thumb, s_right = build_volume_slider(vol, slider_w)
        self.safe_addstr(1, sx, s_left, curses.color_pair(PAIR_CYAN))
        self.safe_addstr(1, sx + len(s_left), s_thumb, curses.color_pair(PAIR_AMBER) | curses.A_BOLD)
        self.safe_addstr(1, sx + len(s_left) + len(s_thumb), s_right, curses.color_pair(PAIR_BOX))
        self.add_hitbox(sx, 1, sx + slider_w, 2, "vol_bar", (sx, slider_w))
        if len(vol_prefix) + slider_w + len(pct_lbl) <= r_in_w:
            px = sx + slider_w
            self.safe_addstr(1, px, pct_lbl, curses.color_pair(PAIR_RED if muted else PAIR_CYAN) | curses.A_BOLD)
            self.add_hitbox(px, 1, px + len(pct_lbl), 2, "mute", None)
        self._draw_header_flags(2, rx + 1, r_in_w, muted)

    def render_rmpc_tabs(self, layout: dict[str, Any]) -> None:
        ty, th, _tx0, tw = layout["tabs"]
        b_attr = curses.color_pair(PAIR_BOX)
        if th >= 2:
            self.safe_addstr(ty, 0, "│", b_attr)
            self.safe_addstr(ty, tw - 1, "│", b_attr)
            self.safe_addstr(ty + 1, 0, "╰" + "─" * max(0, tw - 2) + "╯", b_attr)
            cx = 1
            limit_x = tw - 1
        else:
            self.safe_addstr(ty, 0, " " * tw, curses.color_pair(PAIR_DEFAULT))
            cx = 0
            limit_x = tw

        avail = max(1, limit_x - cx)
        full_w = sum(len(lbl) + 3 for _k, lbl in self.TABS)
        use_compact_lbl = full_w > avail
        show_dividers = (not use_compact_lbl) and (full_w + 6 <= avail)
        div_after = {0, 4, 5}
        dl_jobs = getattr(self, "dl_jobs", [])
        dl_badge = f"D:{len(dl_jobs)}" if dl_jobs else ""
        tab_limit = limit_x - (len(dl_badge) + 3 if dl_badge and avail > 60 else 0)
        for idx, (key, lbl) in enumerate(self.TABS):
            is_act = self.active_tab == key
            if use_compact_lbl:
                short = lbl.split(" ", 1)[1][:2] if " " in lbl else lbl[:2]
                pill = f" {idx + 1}{short} "
            else:
                pill = f" {lbl} "
            pw = len(pill)
            if cx + pw > tab_limit:
                break
            if is_act:
                self.safe_addstr(ty, cx, pill, curses.color_pair(PAIR_PILL_ACTIVE) | curses.A_BOLD)
            elif not use_compact_lbl and " " in lbl:
                num_p, name_p = lbl.split(" ", 1)
                self.safe_addstr(ty, cx, f" {num_p} ", curses.color_pair(PAIR_DIM))
                self.safe_addstr(ty, cx + len(num_p) + 2, f"{name_p} ", curses.color_pair(PAIR_DEFAULT))
            else:
                self.safe_addstr(ty, cx, pill, curses.color_pair(PAIR_DEFAULT))
            self.add_hitbox(cx, ty, cx + pw, ty + 1, "tab", key)
            cx += pw
            if show_dividers and idx in div_after and cx + 2 <= tab_limit:
                self.safe_addstr(ty, cx, "│", b_attr)
                cx += 2
            elif not use_compact_lbl and cx + 1 < tab_limit:
                cx += 1
        if dl_badge and cx + len(dl_badge) + 3 <= limit_x:
            has_run = any(isinstance(j, dict) and j.get("status") == "running" for j in dl_jobs)
            self.draw_button(ty, limit_x - len(dl_badge) - 2, dl_badge, "rtab", "downloads", active=has_run)

    def render_context_bar(self, y: int, w: int) -> None:
        self.safe_addstr(y, 0, " " * w, curses.color_pair(PAIR_DEFAULT))
        bx = 1
        bx = self.draw_button(y, bx, "Bksp <-", "nav_back", None)
        dkind = getattr(self, "drill_kind", "")
        cat_src = getattr(self, "catalog_source", "ytm")
        if dkind == "playlist":
            scope_prefix = "Playlist"
        elif dkind == "local_group":
            scope_prefix = "Local"
        elif dkind == "entity_group":
            scope_prefix = "Library"
        else:
            scope_prefix = cat_src
        dtitle = getattr(self, "drill_title", "") or "Items"
        ctx_lbl = f"{scope_prefix}: {dtitle} ({len(self.tracks)})"

        actions: list[tuple[str, str]] = [("P Play All", "play_all"), ("E +Q All", "add_all"), ("W DL All", "dl_all")]
        if dkind == "playlist":
            actions.append(("S Sync", "sync_pl"))
        btns_w = sum(len(lbl) + 3 for lbl, _ in actions)
        max_ctx_w = max(8, w - bx - btns_w - 2)
        self.safe_addstr(y, bx, truncate_width(ctx_lbl, max_ctx_w), curses.color_pair(PAIR_CYAN) | curses.A_BOLD)

        ax = max(bx + 9, w - btns_w)
        if ax + btns_w <= w:
            for lbl, act in actions:
                ax = self.draw_button(y, ax, lbl, act, None)

    def render_search_bar(self, y: int, w: int) -> None:
        self.safe_addstr(y, 0, " " * w, curses.color_pair(PAIR_DEFAULT))
        cx = 1
        cat_src = getattr(self, "catalog_source", "ytm")
        stype = getattr(self, "search_type", "songs")
        cx = self.draw_button(y, cx, f"C:{cat_src}", "cycle_catalog", None, active=(cat_src != "ytm"))
        if w >= 112:
            for scope_k, scope_lbl in (("songs", "Songs"), ("albums", "Albums"), ("artists", "Artists"), ("playlists", "Lists")):
                attr = curses.color_pair(PAIR_PILL_ACTIVE if stype == scope_k else PAIR_PILL_BTN) | curses.A_BOLD
                btn = f"[{scope_lbl}]"
                self.safe_addstr(y, cx, btn, attr)
                self.add_hitbox(cx, y, cx + len(btn), y + 1, "set_search_type", scope_k)
                cx += len(btn) + 1
        else:
            cx = self.draw_button(y, cx, f"T:{stype}", "cycle_search_type", None, active=(stype != "songs"))

        btn_search_w = 8
        btn_clear_w = 7
        box_x = cx
        box_w = max(10, w - box_x - btn_search_w - btn_clear_w - 4)
        if self.search_focus or self.search_query:
            cursor_ch = "_" if self.search_focus else ""
            raw_in = "/ " + self.search_query + cursor_ch
            inner = truncate_width(raw_in, max(1, box_w - 2))
            box_txt = " " + inner + " " * max(0, box_w - 1 - display_width(inner))
            box_attr = curses.color_pair(PAIR_INPUT) | (curses.A_BOLD if self.search_focus else 0)
        else:
            ph = truncate_width("/ Press / to search...", max(1, box_w - 2))
            box_txt = " " + ph + " " * max(0, box_w - 1 - display_width(ph))
            box_attr = curses.color_pair(PAIR_INPUT)
        self.safe_addstr(y, box_x, box_txt, box_attr)
        self.add_hitbox(box_x, y, box_x + box_w, y + 1, "focus_search", None)

        bx = box_x + box_w + 1
        search_lbl = "..." if getattr(self, "search_loading", False) else "Search"
        bx = self.draw_button(y, bx, search_lbl, "do_search", None, active=bool(getattr(self, "search_loading", False)))
        self.draw_button(y, bx, "Clear", "clear_search", None)

    def _draw_scrollbar_col(self, x: int, y_start: int, total: int, visible: int, offset: int, border_attr: int) -> None:
        glyphs = build_vertical_scrollbar(total, visible, offset, visible)
        for ri, ch in enumerate(glyphs):
            attr = (curses.color_pair(PAIR_CYAN) | curses.A_BOLD) if ch == "█" else border_attr
            self.safe_addstr(y_start + ri, x, ch, attr)

    def _render_lyrics_body(self, y_start: int, body_h: int, x0: int, w: int, border_attr: int) -> None:
        b_inner_w = max(1, w - 4)
        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        d_scroll = max(0, getattr(self, "drawer_scroll", 0))
        if self.lyrics_loading:
            self.safe_addstr(y_start, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_CYAN))
            return
        if not self.lyrics_lines:
            hdr_msg = self.lyrics_header or "Play a track to auto-load lyrics"
            self.safe_addstr(y_start, x0 + 2, truncate_width(hdr_msg, b_inner_w), curses.color_pair(PAIR_DIM))
            if not idle and body_h >= 2:
                mf = extract_metadata_fields(self.mpv_state.get("metadata"))
                ctx_bits = [b for b in (mf.get("artist"), format_album_line(mf.get("album", ""), mf.get("year", 0))) if b]
                if ctx_bits:
                    self.safe_addstr(y_start + 1, x0 + 2, truncate_width(" • ".join(ctx_bits), b_inner_w), curses.color_pair(PAIR_ICE))
            return

        self.safe_addstr(y_start, x0 + 2, truncate_width(self.lyrics_header, b_inner_w), curses.color_pair(PAIR_PURPLE) | curses.A_BOLD)
        tpos = float(self.mpv_state.get("time-pos") or 0.0)
        active_l = 0
        if self.lyrics_lines[0][0] is not None:
            for li, (ts, _txt) in enumerate(self.lyrics_lines):
                if ts is not None and ts <= tpos + 0.15:
                    active_l = li
                else:
                    break
        body_rows = max(0, body_h - 1)
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
            self.safe_addstr(y_start + 1 + row_i, x0 + 2, truncate_width(prefix + ltxt, b_inner_w), attr)
        if body_rows > 0:
            self._draw_scrollbar_col(x0 + w - 1, y_start + 1, len(self.lyrics_lines), body_rows, start_l, border_attr)

    def render_queue_art_lyrics_pane(
        self,
        art_rect: tuple[int, int, int, int] | None,
        lyrics_rect: tuple[int, int, int, int] | None,
    ) -> None:
        focused = getattr(self, "focus_pane", "list") == "drawer"
        b_attr = curses.color_pair(PAIR_CYAN if focused else PAIR_BOX) | (curses.A_BOLD if focused else 0)
        cur_path = str(self.mpv_state.get("path") or "")
        cur_vid = extract_video_id(cur_path) if cur_path else ""
        sel_vid = self.tracks[self.selected_idx][0] if self.tracks and self.selected_idx < len(self.tracks) else ""
        art_vid = cur_vid if VIDEO_ID_RE.match(cur_vid) else sel_vid

        if art_rect is not None:
            ay, ah, ax, aw = art_rect
            self.safe_addstr(ay, ax, "╭" + "─" * max(0, aw - 2) + "╮", b_attr)
            if aw > 14:
                self.safe_addstr(ay, ax + 2, " Album Art ", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            for r in range(1, ah):
                self.safe_addstr(ay + r, ax, "│", b_attr)
                self.safe_addstr(ay + r, ax + aw - 1, "│", b_attr)
            in_h = max(0, ah - 1)
            in_w = max(0, aw - 4)
            art_rows: list[list[tuple[str, int, int]]] = []
            art_sz = calc_queue_art_size(art_rect)
            if self.art_enabled and VIDEO_ID_RE.match(art_vid) and art_sz is not None:
                tw, th = art_sz
                self.ensure_art_async(art_vid, tw, th)
                with self.lock:
                    art_rows = self.art_cache.get(f"{art_vid}:{tw}x{th}", [])
            if art_rows and in_h > 0 and in_w > 0:
                self.reset_art_pairs_for_vid(art_vid)
                draw_rows = art_rows[:in_h]
                art_w = min(in_w, max(len(r) for r in draw_rows))
                sx = ax + 2 + max(0, (in_w - art_w) // 2)
                sy = ay + 1 + max(0, (in_h - len(draw_rows)) // 2)
                for ri, row_cells in enumerate(draw_rows):
                    for ci, (ch, fg, bg) in enumerate(row_cells[:art_w]):
                        try:
                            self.stdscr.addstr(sy + ri, sx + ci, ch, curses.color_pair(self.get_art_pair(fg, bg)))
                        except curses.error:
                            pass
            elif in_h > 0 and in_w > 0:
                lbl = truncate_width("No Cover Art", in_w)
                self.safe_addstr(ay + 1 + in_h // 2, ax + 2 + max(0, (in_w - display_width(lbl)) // 2), lbl, curses.color_pair(PAIR_DIM))
            if lyrics_rect is None:
                self.safe_addstr(ay + ah - 1, ax, "╰" + "─" * max(0, aw - 2) + "╯", b_attr)

        if lyrics_rect is not None:
            ly, lh, lx, lw = lyrics_rect
            top_l = ("├" + "─" * max(0, lw - 2) + "┤") if art_rect is not None else ("╭" + "─" * max(0, lw - 2) + "╮")
            self.safe_addstr(ly, lx, top_l, b_attr)
            if lw > 12:
                self.safe_addstr(ly, lx + 2, " Lyrics ", curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            body_h = max(1, lh - 2)
            self.last_drawer_body_h = body_h
            for r in range(body_h):
                self.safe_addstr(ly + 1 + r, lx, "│", b_attr)
                self.safe_addstr(ly + 1 + r, lx + lw - 1, "│", b_attr)
            self.add_hitbox(lx + 1, ly + 1, lx + lw - 1, ly + lh - 1, "focus_pane", "drawer")
            self._render_lyrics_body(ly + 1, body_h, lx, lw, b_attr)
            self.safe_addstr(ly + lh - 1, lx, "╰" + "─" * max(0, lw - 2) + "╯", b_attr)

    def render_left_pane(self, y0: int, h: int, x0: int, w: int) -> None:
        focused = getattr(self, "focus_pane", "list") == "list"
        border_attr = curses.color_pair(PAIR_CYAN if focused else PAIR_BOX) | (curses.A_BOLD if focused else 0)
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, w - 2) + "╮", border_attr)
        stack = getattr(self, "nav_stack", [])
        if stack:
            title_lbl = truncate_width(getattr(self, "drill_title", "") or "Items", max(8, w - 12))
            hdr = f" {title_lbl} ({len(self.tracks)}) "
            self.safe_addstr(y0, x0 + 2, hdr, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
        else:
            tab_title = dict(self.TABS).get(self.active_tab, self.active_tab.upper())
            hdr = f" {tab_title} " if self.active_tab == "lyrics" else f" {tab_title} ({len(self.tracks)}) "
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

        if self.active_tab == "lyrics" and not stack:
            body_h = max(1, h - 2)
            self.last_list_body_h = body_h
            for row_i in range(body_h):
                ry = y0 + 1 + row_i
                self.safe_addstr(ry, x0, "│", border_attr)
                self.safe_addstr(ry, x0 + w - 1, "│", border_attr)
            self.add_hitbox(x0 + 1, y0 + 1, x0 + w - 1, y0 + h - 1, "focus_pane", "list")
            self._render_lyrics_body(y0 + 1, body_h, x0, w, border_attr)
            self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", border_attr)
            return

        is_group_list = bool(self.tracks and self.tracks[0][3] in GROUP_LIST_TAGS)
        use_4col = w >= 63 and h >= 6 and not is_group_list
        inner_w = max(1, w - 2)
        prefix_w = 6  # "▸ 1.* "

        if use_4col:
            col_w = max(12, inner_w - prefix_w - 1)
            hdr_txt = " " * prefix_w + format_table_header(col_w)
            self.safe_addstr(y0 + 1, x0, "│", border_attr)
            self.safe_addstr(y0 + 1, x0 + 1, truncate_width(hdr_txt, inner_w), curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            self.safe_addstr(y0 + 1, x0 + w - 1, "│", border_attr)
            self.safe_addstr(y0 + 2, x0, "├" + "─" * max(0, w - 2) + "┤", border_attr)
            body_y = y0 + 3
            body_h = max(1, h - 4)
        else:
            body_y = y0 + 1
            body_h = max(1, h - 2)

        self.last_list_body_h = body_h
        if self.selected_idx < self.scroll_top:
            self.scroll_top = self.selected_idx
        elif self.selected_idx >= self.scroll_top + body_h:
            self.scroll_top = self.selected_idx - body_h + 1

        self.add_hitbox(x0 + 1, body_y, x0 + w - 1, y0 + h - 1, "focus_pane", "list")
        for row_i in range(body_h):
            ry = body_y + row_i
            self.safe_addstr(ry, x0, "│", border_attr)
            self.safe_addstr(ry, x0 + w - 1, "│", border_attr)
            idx = self.scroll_top + row_i
            if idx < len(self.tracks):
                vid, title, meta, tag = self.tracks[idx]
                is_sel = idx == self.selected_idx
                marker = "▸" if is_sel else " "
                fav_mark = "*" if vid in self.fav_ids else " "
                prefix = f"{marker}{idx + 1:2d}.{fav_mark} "
                if use_4col:
                    art_s, ttl_s, alb_s, dur_s = parse_track_columns(title, meta, tag)
                    col_w = max(12, inner_w - len(prefix) - 1)
                    text = format_table_row_4col(art_s, ttl_s, alb_s, dur_s, col_w)
                    pad = max(0, inner_w - len(prefix) - display_width(text))
                    line = f"{prefix}{text}{' ' * pad}"
                else:
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
                    msg = " Searching..." if getattr(self, "search_loading", False) else " Empty. Press / or click Search above."
                    self.safe_addstr(ry, x0 + 1, truncate_width(msg, inner_w), curses.color_pair(PAIR_DIM))
                else:
                    self.safe_addstr(ry, x0 + 1, " " * inner_w, curses.color_pair(PAIR_DEFAULT))

        self._draw_scrollbar_col(x0 + w - 1, body_y, len(self.tracks), body_h, self.scroll_top, border_attr)
        self.safe_addstr(y0 + h - 1, x0, "╰" + "─" * max(0, w - 2) + "╯", border_attr)

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
                    _qvid, qtitle, _qmeta = self._resolve_queue_entry(entry, qi)
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

    def render_hint_bar(self, y: int, w: int) -> None:
        self.safe_addstr(y, 0, " " * w, curses.color_pair(PAIR_DEFAULT))
        is_grp = bool(self.tracks and self.tracks[0][3] in GROUP_LIST_TAGS)
        hints = build_context_hints(
            self.active_tab,
            search_focus=bool(getattr(self, "search_focus", False)),
            active_modal=str(getattr(self, "active_modal", "")),
            has_nav=bool(getattr(self, "nav_stack", None)),
            is_group_list=is_grp,
        )
        cx = 1
        for k_txt, lbl in hints:
            need = len(k_txt) + 1 + len(lbl) + 2
            if cx + need > w:
                break
            self.safe_addstr(y, cx, k_txt, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
            cx += len(k_txt) + 1
            self.safe_addstr(y, cx, lbl, curses.color_pair(PAIR_DIM))
            cx += len(lbl) + 2

    def render_rmpc_footer(self, layout: dict[str, Any]) -> None:
        fy, fh, _fx0, fw = layout["footer"]
        _, _, _mx0, mw = layout["footer_mode"]
        _, _, _px0, pw = layout["footer_progress"]

        pos = float(self.mpv_state.get("time-pos") or 0.0)
        dur = float(self.mpv_state.get("duration") or 0.0)
        pl = self.mpv_state.get("playlist") or []

        dl_active = any(isinstance(j, dict) and j.get("status") == "running" for j in getattr(self, "dl_jobs", []))
        cur_modal = getattr(self, "active_modal", "")
        if getattr(self, "search_focus", False):
            mode_lbl = "INPUT"
            mode_pair = PAIR_GREEN
            mode_act: tuple[str, Any] = ("focus_search", None)
        elif cur_modal in ("downloads", "info"):
            mode_lbl = "MODAL"
            mode_pair = PAIR_PURPLE
            mode_act = ("close_modal", None)
        elif dl_active:
            mode_lbl = "DL"
            mode_pair = PAIR_AMBER
            mode_act = ("rtab", "downloads")
        else:
            mode_lbl = "NORMAL"
            mode_pair = PAIR_PILL_ACTIVE
            mode_act = ("focus_search", None)

        summary = format_queue_summary(pl, dur)
        has_toast = bool(self.toast_msg and time.monotonic() < self.toast_until)
        right_title = f"{self.toast_msg} | {summary}" if has_toast else summary
        b_attr = curses.color_pair(PAIR_BOX)

        def draw_timeline(ty: int, tx: int, tw: int) -> None:
            pos_s = format_mmss(pos)
            dur_s = format_mmss(dur)
            if tw >= 24:
                self.safe_addstr(ty, tx, pos_s, curses.color_pair(PAIR_CYAN) | curses.A_BOLD)
                self.safe_addstr(ty, tx + tw - len(dur_s), dur_s, curses.color_pair(PAIR_CYAN))
                bar_x = tx + len(pos_s) + 2
                bar_w = max(4, tw - len(pos_s) - len(dur_s) - 4)
            else:
                bar_x = tx
                bar_w = max(4, tw)
            if dur > 0 and bar_w >= 4:
                ratio = max(0.0, min(1.0, pos / dur))
                knob_idx = min(bar_w - 1, max(0, int(round(ratio * (bar_w - 1)))))
                left_track = "━" * knob_idx
                right_track = "─" * max(0, bar_w - knob_idx - 1)
                if left_track:
                    self.safe_addstr(ty, bar_x, left_track, curses.color_pair(PAIR_BAR_FILL) | curses.A_BOLD)
                self.safe_addstr(ty, bar_x + knob_idx, "●", curses.color_pair(PAIR_AMBER) | curses.A_BOLD)
                if right_track:
                    self.safe_addstr(ty, bar_x + knob_idx + 1, right_track, curses.color_pair(PAIR_DIM))
            else:
                self.safe_addstr(ty, bar_x, "─" * bar_w, curses.color_pair(PAIR_DIM))
            self.add_hitbox(bar_x, ty, bar_x + bar_w, ty + 1, "seek_bar", (bar_x, bar_w, dur))

        if fh >= 3:
            top_line = "╭" + "─" * max(0, mw - 1) + "┬" + "─" * max(0, pw - 2) + "╮"
            bot_line = "╰" + "─" * max(0, mw - 1) + "┴" + "─" * max(0, pw - 2) + "╯"
            self.safe_addstr(fy, 0, top_line, b_attr)
            if pw > 6:
                title_txt = f" {truncate_width(right_title, max(4, pw - 4))} "
                tx = max(mw + 1, fw - 1 - display_width(title_txt))
                t_attr = curses.color_pair(PAIR_TOAST if has_toast else PAIR_CYAN) | (curses.A_BOLD if has_toast else 0)
                self.safe_addstr(fy, tx, title_txt, t_attr)

            self.safe_addstr(fy + 1, 0, "│", b_attr)
            self.safe_addstr(fy + 1, mw, "│", b_attr)
            self.safe_addstr(fy + 1, fw - 1, "│", b_attr)

            m_in_w = max(1, mw - 2)
            m_txt = truncate_width(f" {mode_lbl} ", m_in_w)
            m_pad = max(0, (m_in_w - display_width(m_txt)) // 2)
            self.safe_addstr(fy + 1, 1, " " * m_in_w, curses.color_pair(mode_pair))
            self.safe_addstr(fy + 1, 1 + m_pad, m_txt, curses.color_pair(mode_pair) | curses.A_BOLD)
            self.add_hitbox(1, fy + 1, mw, fy + 2, mode_act[0], mode_act[1])

            draw_timeline(fy + 1, mw + 2, max(4, pw - 4))
            self.safe_addstr(fy + 2, 0, bot_line, b_attr)
        else:
            self.safe_addstr(fy, 0, "─" * fw, b_attr)
            if fw > 10:
                title_txt = f" {truncate_width(right_title, max(4, fw - 4))} "
                tx = max(0, fw - display_width(title_txt) - 1)
                t_attr = curses.color_pair(PAIR_TOAST if has_toast else PAIR_CYAN) | (curses.A_BOLD if has_toast else 0)
                self.safe_addstr(fy, tx, title_txt, t_attr)
            m_pill = f" {mode_lbl} "
            self.safe_addstr(fy + 1, 0, m_pill, curses.color_pair(mode_pair) | curses.A_BOLD)
            self.add_hitbox(0, fy + 1, len(m_pill), fy + 2, mode_act[0], mode_act[1])
            draw_timeline(fy + 1, len(m_pill) + 1, max(4, fw - len(m_pill) - 2))

    def render_overlay_modal(self, h: int, w: int, kind: str) -> None:
        mw = min(max(36, w - 4), 76)
        mh = min(max(8, h - 4), 18)
        x0 = max(1, (w - mw) // 2)
        y0 = max(1, (h - mh) // 2)
        self.add_hitbox(0, 0, w, h, "close_modal", None)
        self.add_hitbox(x0, y0, x0 + mw, y0 + mh, "noop", None)

        b_attr = curses.color_pair(PAIR_CYAN) | curses.A_BOLD
        self.safe_addstr(y0, x0, "╭" + "─" * max(0, mw - 2) + "╮", b_attr)
        inner_w = max(1, mw - 2)
        for row_i in range(1, mh - 1):
            self.safe_addstr(y0 + row_i, x0, "│", b_attr)
            self.safe_addstr(y0 + row_i, x0 + 1, " " * inner_w, curses.color_pair(PAIR_DEFAULT))
            self.safe_addstr(y0 + row_i, x0 + mw - 1, "│", b_attr)
        self.safe_addstr(y0 + mh - 1, x0, "╰" + "─" * max(0, mw - 2) + "╯", b_attr)

        jobs = list(reversed(getattr(self, "dl_jobs", [])))
        title_txt = f" DOWNLOADS ({len(jobs)}) " if kind == "downloads" else " TRACK INFO "
        self.safe_addstr(y0, x0 + 2, truncate_width(title_txt, max(4, mw - 16)), b_attr)
        if mw >= 20:
            self.draw_button(y0, x0 + mw - 12, "Esc Close", "close_modal", None)

        b_in_w = max(1, mw - 4)
        b_in_h = max(1, mh - 2)
        d_scroll = max(0, getattr(self, "drawer_scroll", 0))
        if kind == "downloads":
            if not jobs:
                self.safe_addstr(y0 + 1, x0 + 2, truncate_width("No active or recent downloads. Press [d] on a track.", b_in_w), curses.color_pair(PAIR_DIM))
                return
            self.draw_button(y0 + 1, x0 + 2, "X Cancel All", "dl_cancel", None)
            body_rows = max(1, b_in_h - 1)
            start_j = min(max(0, len(jobs) - body_rows), d_scroll)
            for ri in range(body_rows):
                ji = start_j + ri
                if ji >= len(jobs):
                    break
                j = jobs[ji]
                st = str(j.get("status") or "running")
                pct = int(j.get("pct") or 0)
                badge = "[DONE]" if st == "done" else ("[SKIP]" if st == "skipped" else ("[ERR]" if st == "error" else f"[{pct:3d}%]"))
                pair_j = PAIR_GREEN if st == "done" else (PAIR_DIM if st == "skipped" else (PAIR_RED if st == "error" else PAIR_CYAN))
                line = truncate_width(f"{badge} {j.get('title') or ''} ({j.get('stage') or ''})", b_in_w)
                self.safe_addstr(y0 + 2 + ri, x0 + 2, line, curses.color_pair(pair_j))
            self._draw_scrollbar_col(x0 + mw - 1, y0 + 2, len(jobs), body_rows, start_j, b_attr)
            return

        if not self.tracks or not (0 <= self.selected_idx < len(self.tracks)):
            self.safe_addstr(y0 + 1, x0 + 2, "No track selected", curses.color_pair(PAIR_DIM))
            return
        svid, stitle, smeta, stag = self.tracks[self.selected_idx]
        cur_path = str(self.mpv_state.get("path") or "")
        cur_vid = extract_video_id(cur_path) if cur_path else ""
        idle = not self.mpv_state or bool(self.mpv_state.get("idle-active", True))
        if not idle and svid and (svid == cur_vid or svid == cur_path):
            mf = extract_metadata_fields(self.mpv_state.get("metadata"))
            sel_alb = format_album_line(mf["album"], mf["year"])
        else:
            sel_alb = extract_album_from_meta(smeta)
        info_rows: list[tuple[str, int]] = [(f"Title: {stitle}", curses.color_pair(PAIR_TITLE) | curses.A_BOLD)]
        if sel_alb:
            info_rows.append((f"Album: {sel_alb}", curses.color_pair(PAIR_ICE)))
        info_rows.append((f"Info : {smeta or '-'}", curses.color_pair(PAIR_ICE)))
        info_rows.append((f"ID   : {svid} {stag}", curses.color_pair(PAIR_PURPLE)))
        max_rows = max(1, b_in_h - 1)
        drawn = 0
        for txt, attr in info_rows[:max_rows]:
            self.safe_addstr(y0 + 1 + drawn, x0 + 2, truncate_width(txt, b_in_w), attr)
            drawn += 1
        if b_in_h >= drawn + 1:
            ax = x0 + 2
            ay = y0 + 1 + drawn
            for b_lbl, b_act in (("Ent Play", "play_sel"), ("a +Q", "add_sel"), ("r Radio", "radio_sel"), ("f Fav", "fav_sel"), ("d DL", "dl_sel")):
                if ax + len(b_lbl) + 3 <= x0 + mw - 1:
                    ax = self.draw_button(ay, ax, b_lbl, b_act)
            if stag == "[Q]" and b_in_h >= drawn + 2:
                qx = x0 + 2
                qx = self.draw_button(ay + 1, qx, "K Up", "q_up")
                self.draw_button(ay + 1, qx, "J Down", "q_down")

    def render_help_modal(self, h: int, w: int) -> None:
        lines = [
            "MUSICCTL v2.4 - HYBRID RMPC KEYBOARD + MOUSE CONTROLS",
            "",
            "Mouse:",
            "  Click / Double / Right  Select / Play or Open / Enqueue track",
            "  Bars / Tabs / Wheel     Header pills, Vol, Seek, Tabs / Scroll pane",
            "",
            "Layout & Navigation:",
            "  1-8 / Tab / Shift-Tab   Tabs (1 Queue .. 8 Lyrics) / reverse",
            "  h/l j/k PgUp/Dn C-U/D   Focus pane / Move, page, g/G top-end",
            "  v / z / D / I (Esc)     Cycle layout / Side pane / DL & Info modals",
            "  / C-S (C-L)  C  T  L    Search | Catalog / Scope / LocalGrp | Bksp",
            "",
            "Playback, Batch & Queue:",
            "  Space / p   < / >       Play-Pause | Prev / Next (also b / n)",
            "  , / . (-/+) [/] s / x   Vol -/+5% | Seek -/+10s | Stop daemon",
            "  Ent / a / f / r / d     Play or Open / +Q / Fav / Radio / Download",
            "  P / E / W / S / X       Play All / +Q All / DL All / Sync / Cancel",
            "  J/K  o u Q e N A m ? q  Move | Loop Shuf Qual EQ Norm Auto Mute ? q",
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

        has_nav = bool(getattr(self, "nav_stack", None))
        show_top_bar = (self.active_tab == "search") or has_nav
        layout = compute_rmpc_layout(
            h,
            w,
            getattr(self, "layout_mode", "auto"),
            getattr(self, "show_drawer", True),
            search_bar=show_top_bar,
            active_tab=self.active_tab,
        )
        self.last_layout = layout
        if layout["drawer"] is None and layout["lyrics"] is None and getattr(self, "focus_pane", "list") == "drawer":
            self.focus_pane = "list"

        self.render_rmpc_header(layout)
        self.render_rmpc_tabs(layout)
        if layout["search_bar"] is not None:
            sy, _sh, _sx, sw = layout["search_bar"]
            if has_nav:
                self.render_context_bar(sy, sw)
            else:
                self.render_search_bar(sy, sw)

        if layout["art"] is not None or layout["lyrics"] is not None:
            self.render_queue_art_lyrics_pane(layout["art"], layout["lyrics"])
        self.render_left_pane(*layout["list"])
        if layout["drawer"] is not None:
            self.render_drawer_card(*layout["drawer"])
        if layout.get("hint_bar") is not None:
            hy, _hh, _hx, hw = layout["hint_bar"]
            self.render_hint_bar(hy, hw)

        self.render_rmpc_footer(layout)
        if getattr(self, "active_modal", "") in ("downloads", "info"):
            self.render_overlay_modal(h, w, self.active_modal)
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
        return [
            vid
            for vid, _t, _m, tag in self.tracks
            if vid and classify_row(vid, tag) not in ("entity_group", "local_group", "catalog_container", "playlist")
        ]

    def _focus_downloads_view(self, toast: str = "") -> None:
        self.right_tab = "downloads"
        if toast:
            self.set_toast(toast)
        layout = getattr(self, "last_layout", None)
        if layout and layout.get("drawer") is None:
            self.active_modal = "downloads"

    def _ensure_lyrics_for_current(self) -> None:
        title = str(self.mpv_state.get("media-title") or "")
        cur_path = str(self.mpv_state.get("path") or "")
        artist = extract_primary_artist(self.mpv_state.get("metadata"))
        dur = float(self.mpv_state.get("duration") or 0.0)
        if cur_path and cur_path != self.lyrics_track_key and not self.lyrics_loading:
            if is_placeholder_title(title, extract_video_id(cur_path)):
                self.lyrics_header = "Waiting for track metadata..."
            else:
                self.fetch_lyrics_async(title, cur_path, artist=artist, duration=dur)

    def _maybe_requery_search(self) -> bool:
        q = (getattr(self, "search_query", "") or "").strip()
        if q and self.active_tab == "search":
            self.trigger_search(q)
            return True
        return False

    def play_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        kind = classify_row(vid, tag)
        if kind == "entity_group":
            rows = getattr(self, "_entity_groups", {}).get(vid, [])
            self.push_nav(title, rows, context_ref=vid, kind="entity_group")
        elif kind == "local_group":
            rows = self._local_group_to_rows(vid)
            self.push_nav(title, rows, context_ref=vid, kind="local_group")
        elif kind == "playlist":
            pl_name = vid[3:]
            rows = self.read_tsv(self.data_dir / "playlists" / f"{pl_name}.tsv", "[TRK]")
            self.push_nav(pl_name, rows, context_ref=vid, kind="playlist")
        elif kind == "catalog_container":
            self.trigger_browse(vid, title)
        elif kind == "queue_item":
            self.mpv.send(["set_property", "playlist-pos", self.selected_idx])
            self.mpv.send(["set_property", "pause", False])
            self.set_toast(f"Jump #{self.selected_idx + 1}")
        elif kind == "catalog_track":
            self.run_cli_async(["play", vid], toast=f"Playing: {title[:24]}", reload_after=True)
        else:
            line = f"{vid}\t{title}\t{meta}\t{tag}"
            self.run_cli_async(["_play_line", line], toast=f"Playing: {title[:24]}", reload_after=True)

    def enqueue_selected(self) -> None:
        if not self.tracks or self.selected_idx >= len(self.tracks):
            return
        vid, title, meta, tag = self.tracks[self.selected_idx]
        kind = classify_row(vid, tag)
        if kind == "entity_group":
            refs = [r[0] for r in getattr(self, "_entity_groups", {}).get(vid, []) if r[0]]
            if refs:
                self.run_cli_batch_async([["add", r] for r in refs], toast=f"Queued {len(refs)} tracks", reload_after=True)
        elif kind == "local_group":
            paths = [str(e.get("path") or "") for e in getattr(self, "_local_groups", {}).get(vid, []) if e.get("path")]
            if paths:
                self.run_cli_batch_async([["add", p] for p in paths], toast=f"Queued {len(paths)} tracks", reload_after=True)
        elif kind in ("playlist", "catalog_container", "catalog_track"):
            self.run_cli_async(["add", vid], toast=f"Queued: {title[:24]}", reload_after=True)
        else:
            line = f"{vid}\t{title}\t{meta}\t{tag}"
            self.run_cli_async(["_add_line", line], toast=f"Queued: {title[:24]}", reload_after=True)

    def handle_action(self, action: str, payload: Any, bstate: int = 0) -> None:
        if action == "noop":
            return
        if action == "close_modal":
            self.active_modal = ""
            return
        if action == "close_help":
            self.show_help = False
            return
        if action == "tab":
            self.search_focus = False
            self.active_modal = ""
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
            if not self._maybe_requery_search():
                self.set_toast(f"Catalog: {nxt}")
        elif action == "cycle_search_type":
            nxt = self.cycle_search_type()
            if not self._maybe_requery_search():
                self.set_toast(f"Type: {nxt}")
        elif action == "set_search_type":
            if payload in self.SEARCH_TYPES:
                self.search_type = payload
                if not self._maybe_requery_search():
                    self.set_toast(f"Type: {payload}")
        elif action == "cycle_local_group":
            nxt = self.cycle_local_group()
            self.set_toast(f"Local view: {nxt}")
        elif action == "nav_back":
            self.pop_nav()
        elif action == "play_all":
            ctx = getattr(self, "drill_ref", "")
            refs = self._collect_track_refs()
            if ctx and not ctx.startswith(("lart:", "lalb:", "eart:", "ealb:")):
                self.run_cli_async(["play", ctx], toast=f"Playing {self.drill_title[:20]}", reload_after=True)
            elif refs:
                cmds = [["play", refs[0]], *(["add", r] for r in refs[1:])]
                self.run_cli_batch_async(cmds, toast=f"Playing {len(refs)} tracks", reload_after=True)
        elif action == "add_all":
            ctx = getattr(self, "drill_ref", "")
            refs = self._collect_track_refs()
            if ctx and not ctx.startswith(("lart:", "lalb:", "eart:", "ealb:")):
                self.run_cli_async(["add", ctx], toast=f"Queued {self.drill_title[:20]}", reload_after=True)
            elif refs:
                cmds = [["add", r] for r in refs]
                self.run_cli_batch_async(cmds, toast=f"Queued {len(refs)} tracks", reload_after=True)
        elif action == "dl_all":
            ctx = getattr(self, "drill_ref", "")
            ctx_kind = classify_row(ctx, "") if ctx else ""
            dl_mgr = getattr(self, "dl_manager", None)
            if ctx and ctx_kind in ("playlist", "catalog_container"):
                if dl_mgr:
                    dl_mgr.start([ctx], label=self.drill_title or ctx)
                self._focus_downloads_view(f"Downloading: {(self.drill_title or ctx)[:24]}")
            else:
                refs = [vid for vid, _t, _m, tag in self.tracks if is_downloadable_ref(vid, tag)]
                if not refs:
                    self.set_toast("Already local")
                else:
                    if dl_mgr:
                        dl_mgr.start(refs, label=self.drill_title or f"{len(refs)} tracks")
                    self._focus_downloads_view(f"Downloading {len(refs)} track(s)")
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
                    self._focus_downloads_view(f"Syncing {pl_name}...")
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
            if payload in ("downloads", "info") and getattr(self, "last_layout", {}).get("drawer") is None:
                cur_m = getattr(self, "active_modal", "")
                self.active_modal = "" if cur_m == payload else payload
                if self.active_modal:
                    self.focus_pane = "drawer"
            else:
                self.active_modal = ""
                self.show_drawer = True
                self.focus_pane = "drawer"
                if payload == "lyrics":
                    self._ensure_lyrics_for_current()
        elif action == "focus_search":
            if self.active_tab != "search":
                self.handle_action("tab", "search")
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
                kind = classify_row(vid, tag)
                if kind in ("local_group", "local_file"):
                    self.set_toast("Already local")
                    return
                dl_mgr = getattr(self, "dl_manager", None)
                if kind == "entity_group":
                    refs = [r[0] for r in getattr(self, "_entity_groups", {}).get(vid, []) if is_downloadable_ref(r[0], r[3])]
                    if not refs:
                        self.set_toast("Already local")
                        return
                    if dl_mgr:
                        dl_mgr.start(refs, label=title or vid)
                        self.set_toast(f"Downloading {len(refs)} track(s): {title[:20]}")
                    else:
                        self.run_cli_async(["download", *refs], toast="Download complete", reload_after=True, timeout=300.0)
                    return
                if dl_mgr:
                    dl_mgr.start([vid], label=title or vid)
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
        if (target == "list" and self.active_tab == "lyrics" and not getattr(self, "nav_stack", None)) or (
            target == "drawer" and getattr(self, "last_layout", {}).get("lyrics") is not None
        ):
            total = len(getattr(self, "lyrics_lines", []) or [])
            bh_src = getattr(self, "last_list_body_h", 12) if target == "list" else getattr(self, "last_drawer_body_h", 8)
            body_h = max(1, bh_src - 1)
            max_sc = max(0, total - body_h)
            cur_sc = getattr(self, "drawer_scroll", 0)
            self.drawer_scroll = max(0, min(max_sc, cur_sc + delta))
            return
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

        cur_modal = getattr(self, "active_modal", "")
        if cur_modal in ("downloads", "info"):
            if ch in (27, ord("q")):
                self.active_modal = ""
                return True
            if ch == ord("?"):
                self.active_modal = ""
                self.show_help = True
                return True
            if ch == ord("D"):
                self.handle_action("rtab", "downloads")
                return True
            if ch == ord("I"):
                self.handle_action("rtab", "info")
                return True
            if ch == ord("U"):
                self.active_modal = ""
                self.handle_action("rtab", "upnext")
                return True
            if ch == ord("Y"):
                self.active_modal = ""
                self.handle_action("rtab", "lyrics")
                return True
            if ch in (ord("j"), curses.KEY_DOWN):
                self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) + 1)
                return True
            if ch in (ord("k"), curses.KEY_UP):
                self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) - 1)
                return True
            if ch in (curses.KEY_NPAGE, 4):
                self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) + 6)
                return True
            if ch in (curses.KEY_PPAGE, 21):
                self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) - 6)
                return True
            if ch in (ord("g"), curses.KEY_HOME):
                self.drawer_scroll = 0
                return True
            if ch in (ord("G"), curses.KEY_END):
                self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) + 50)
                return True
            if ch == ord("X") and cur_modal == "downloads":
                self.handle_action("dl_cancel", None)
                return True
            if cur_modal == "info":
                if ch in (10, 13, curses.KEY_ENTER):
                    self.handle_action("play_sel", None)
                    return True
                for k_ch, m_act in ((ord("a"), "add_sel"), (ord("r"), "radio_sel"), (ord("f"), "fav_sel"), (ord("d"), "dl_sel")):
                    if ch == k_ch:
                        self.handle_action(m_act, None)
                        return True
            return True

        if ch in (ord("q"), 3):
            return False
        if ch in (ord("/"), 19):  # / or Ctrl-S
            if self.active_tab != "search":
                self.handle_action("tab", "search")
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
        elif ch in (ord(" "), ord("p")):
            self.handle_action("toggle", None)
        elif ch in (ord("n"), ord(">")):
            self.handle_action("next", None)
        elif ch in (ord("b"), ord("<")):
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
        elif ch in (ord("x"), ord("s")):
            self.handle_action("stop", None)
        elif ch in (ord("+"), ord("="), ord(".")):
            self.handle_action("vol_delta", 5)
        elif ch in (ord("-"), ord(",")):
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
        elif ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5"), ord("6"), ord("7"), ord("8")):
            idx = ch - ord("1")
            if getattr(self, "focus_pane", "list") == "drawer" and idx < len(self.RIGHT_TABS):
                self.handle_action("rtab", self.RIGHT_TABS[idx][0])
            elif idx < len(self.TABS):
                self.focus_pane = "list"
                self.handle_action("tab", self.TABS[idx][0])
        elif ch == 9:  # Tab cycles active pane tabs
            if getattr(self, "focus_pane", "list") == "drawer":
                cur_r = next((i for i, (k, _) in enumerate(self.RIGHT_TABS) if k == self.right_tab), 0)
                self.handle_action("rtab", self.RIGHT_TABS[(cur_r + 1) % len(self.RIGHT_TABS)][0])
            else:
                cur_t = next((i for i, (k, _) in enumerate(self.TABS) if k == self.active_tab), 0)
                self.handle_action("tab", self.TABS[(cur_t + 1) % len(self.TABS)][0])
        elif ch == getattr(curses, "KEY_BTAB", 353):
            if getattr(self, "focus_pane", "list") == "drawer":
                cur_r = next((i for i, (k, _) in enumerate(self.RIGHT_TABS) if k == self.right_tab), 0)
                self.handle_action("rtab", self.RIGHT_TABS[(cur_r - 1) % len(self.RIGHT_TABS)][0])
            else:
                cur_t = next((i for i, (k, _) in enumerate(self.TABS) if k == self.active_tab), 0)
                self.handle_action("tab", self.TABS[(cur_t - 1) % len(self.TABS)][0])
        return True

    def handle_mouse_wheel(self, mx: int, my: int, delta: int) -> None:
        if getattr(self, "active_modal", "") in ("downloads", "info"):
            self.drawer_scroll = max(0, getattr(self, "drawer_scroll", 0) + delta)
            return
        layout = getattr(self, "last_layout", {}) or {}
        dr = layout.get("drawer")
        ly = layout.get("lyrics")
        in_side = bool(
            (dr and dr[2] <= mx < dr[2] + dr[3] and dr[0] <= my < dr[0] + dr[1])
            or (ly and ly[2] <= mx < ly[2] + ly[3] and ly[0] <= my < ly[0] + ly[1])
        )
        self.scroll_active_pane(delta, pane="drawer" if in_side else "list")

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
                    if self._queue_state_sig() != self._last_queue_sig:
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
                    self.handle_mouse_wheel(mx, my, -3)
                    continue
                if bstate & curses.BUTTON5_PRESSED:
                    self.handle_mouse_wheel(mx, my, 3)
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
