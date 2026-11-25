# WORK PACKET 2: v2.2.0 MULTI-CATALOG, AUDIOPHILE AUDIO & PRODUCTION DOWNLOAD BACKEND

- **Track**: Track B / Spec-Driven Incremental Delivery (Zero-Bloat CLI & TUI)
- **Status**: COMPLETED [9/9 CHUNKS]
- **Obsidian Namespace**: `00-AGY-Memory/musicctl` (`INDEX`, `CONTEXT`, `STATE`, `DECISIONS`)
- **Base Commit**: `a72e23fab524ce5ba982ac6c1aa49bd99d092848` (`v2.1.0` + pre-WP2 hardening)
- **Verified Baseline**: `make lint` (`shellcheck -S warning`) exit 0 | `test_musicctl.sh` 153/153 offline PASS | `test_tui.py` 20/20 OK

---

## 1. Objectives & Architectural Boundaries

1. **Preserve Lightweight Footprint**: Maintain ~20-30 MB RAM runtime using Bash + Python 3 stdlib (`lib/*.py`) + `mpv` IPC + embedded `mpv` Lua hook (`lib/mpv_hooks.lua`, zero extra persistent processes) + `yt-dlp` + `ffmpeg`/`ffprobe`. Strictly reject hidden WebView/browser engines, Electron, or mandatory third-party pip dependencies (`ADR-002`, `ADR-006`, `ADR-009`).
2. **Decoupled 2-Layer Multi-Catalog & Audio Architecture**: Separate the **Catalog/Metadata Layer** (`lib/catalog.py`: YTM InnerTube + zero-key public Deezer & iTunes adapters + canonical `{source, id, type, isrc, title, artist, album, track_num, year, duration, cover_url}` schema) from the **Audio Delivery Layer** (lazy Python SSOT provider module `lib/providers.py` -> YTM / `yt-dlp` universal fallback), unlocking clean album/artist/playlist drill-down and ISRC-backed lossless matching.
3. **Audiophile & Popular CLI Tool Parity**: Match ceiling capabilities of `yt-dlp`, `spotDL`, `Streamrip`, `Orpheon`, `SimpMusic`, and `beets` via zero generation-loss remuxing, strict lossless/Hi-Res `ffprobe` validation, MPEG-DASH FLAC manifest support, `soxr` resampling, SponsorBlock auto-skip, infinite autoplay radio, batch concurrent downloads, local library indexing, and UNIX lifecycle hooks.

---

## 2. Active Work Packet (Max 10 Chunks)

### [x] Chunk WP2A: Multi-Catalog Entity Engine (`lib/catalog.py`)
- **Target Files**: `lib/catalog.py` (new), `bin/musicctl`, `tests/test_tui.py`, `tests/test_musicctl.sh`
- **Steps**:
  1. Implement `lib/catalog.py` (Python 3 stdlib only) with a canonical entity schema (`{source, id, type, isrc, title, artist, album, track_num, year, duration, cover_url}`) and zero-key catalog adapters:
     - `ytm` (default): InnerTube `WEB_REMIX` typed search (`songs|albums|artists|playlists`) + `browse` (`MPREb_*`, `UC*`, `VLPL*`, continuation paging).
     - `deezer`: public `api.deezer.com` search/album/artist/playlist adapter supplying clean metadata, **ISRC**, and 1000x1000 square cover art.
     - `itunes`: public `itunes.apple.com` search/lookup fallback for clean album/artist catalogs.
  2. Add `MUSICCTL_CATALOG=ytm|deezer|itunes` config key (default `ytm`) and wire CLI commands in `bin/musicctl`: `musicctl search [--catalog <src>] [--type songs|albums|artists|playlists] <query>`, `musicctl browse <ref>`, and `musicctl play|add <ref>` (resolving non-YTM tracks through Audio Provider Chain -> YTM fallback, plus universal URL input for Spotify, SoundCloud, and Bandcamp via `yt-dlp`).
- **Definition of Done (DoD)**:
  - Offline JSON fixture tests verify canonical schema mapping and `search`/`browse` parsing across `ytm` (`MPREb_*`, `UC*`, `VLPL*`), `deezer` (with ISRC + square art), and `itunes`; `make lint && make test` exits `0`.

### [x] Chunk WP2B: End-to-End Album, ISRC & Clean Metadata Propagation
- **Target Files**: `bin/musicctl`, `lib/common.py`, `lib/tui.py`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Add `parse_meta_album` in `bin/musicctl` (and extract `album`, `isrc`, `cover_url`, `track_num`, `year` from `lib/catalog.py` / `mpv` `metadata/by-key/album`) instead of passing `""`, plus clean title normalization (stripping `(Official Video)` / `(Lyrics)` noise for provider/lyrics matching).
  2. Propagate `album`, `isrc`, `track_num`, and `year` into `resolve_provider_audio`, `embed_metadata_cover`, `transcode_small_audio`, and `cmd_status --json`.
  3. Display `album` (and `year` if available) in `lib/tui.py` `NOW PLAYING` and `Info` cards.
- **Definition of Done (DoD)**:
  - Provider JSON stdin test verifies non-empty `album`, `isrc`, and clean normalized title; `ffprobe` test confirms `album` tag written into `.flac` and `.m4a` downloads.

### [x] Chunk WP2C: Audiophile Playback Engine, SponsorBlock & Infinite Autoplay
- **Target Files**: `lib/mpv_hooks.lua` (new), `bin/musicctl`, `lib/common.py`, `lib/tui.py`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Upgrade YTM stream format priority to `774/251/141/bestaudio[acodec=opus]/bestaudio/best` with opt-in `MUSICCTL_YTM_COOKIES` (supporting YTM Premium 256k Opus/AAC while preserving `--ignore-config` default for anonymous users).
  2. Tune `mpv` audiophile defaults: enable `soxr` high-precision resampler (`--audio-resample-filter=soxr` / `--swresample-opts` when supported), preserve native Hi-Res sample rates when `norm=off` (removing forced 48 kHz downsample outside `loudnorm`), and add `MUSICCTL_REPLAYGAIN=track|album|no`.
  3. Add event-driven `lib/mpv_hooks.lua` (loaded optionally by `ensure_daemon()` via `--script`, degrading gracefully if absent) for full CLI + TUI parity on **SponsorBlock** (`MUSICCTL_SPONSORBLOCK=1`, public `sponsor.ajay.app` API with 2s timeout, skipping `music_offtopic` / non-music segments on `file-loaded` / `time-pos` + `yt-dlp --sponsorblock-remove music_offtopic` fallback) and **Infinite Autoplay** (`musicctl autoplay [on|off|toggle]` + `MUSICCTL_AUTOPLAY`: prefetching Up Next Radio via `musicctl _autoplay_seed <vid>` on last-track `file-loaded` when loop is off, guarded by `last_seeded_vid` for gapless transitions).
- **Definition of Done (DoD)**:
  - Offline tests verify SponsorBlock segment JSON parsing, `ytm_format_for_quality` `774/251/141` priority, `MUSICCTL_REPLAYGAIN` arg generation, and `autoplay` queue append guard; `make lint && make test` exits `0`.

### [x] Chunk WP2D: Production Download Engine (`lib/downloader.py`) & Lazy Provider SSOT (`lib/providers.py`)
- **Target Files**: `lib/providers.py` (new), `lib/downloader.py` (new), `bin/musicctl`, `tests/test_tui.py`, `tests/test_musicctl.sh`
- **Steps**:
  1. Extract `lib/providers.py` (Python 3 stdlib) as the single source of truth for provider resolution (`iter_provider_candidates` lazy generator short-circuiting on first valid match instead of eagerly executing all scripts), stream probing, `ffprobe` validation, and `hires -> cd -> YTM` cascade — shared in-process by `lib/downloader.py` (`--jobs 1..4`) and via a single CLI entrypoint from `bin/musicctl` (`load_track_target`).
  2. Build `lib/downloader.py` (Python 3 stdlib + `yt-dlp`/`ffmpeg`/`ffprobe` subprocesses) supporting canonical catalog refs (`ytm`/`deezer`/`itunes` singles, albums, playlists, artists), Spotify/SoundCloud/Bandcamp URLs, and local named playlists (`pl:<name>`).
  3. Add CLI flags on `musicctl dl|download`: `--quality small|cd|hires|ask`, `--format auto|flac|opus|m4a|mp3`, `--jobs <1..4>` (`ThreadPoolExecutor`), `--template <pattern>` (default `{artist}/{album}/{track:02d} - {title}`, config key `MUSICCTL_DL_TEMPLATE`), `--lrc` (save synced/plain `.lrc` sidecar), `--replaygain`, `--force`, `--dry-run`, and `dl sync <source> [--prune]` folder mirroring.
  4. Enforce zero generation-loss remux (`--audio-format best` on `--format auto`; transcode only on explicit `--format`), default `libopus` 96-128k for `small` tier (with `--format m4a` for AAC), skip-existing archive (`$MUSICCTL_DATA_DIR/download-archive.tsv`), atomic temp staging + strict `ffprobe` tier gate (`cd` requires lossless `flac`/`alac`/`wav`/`pcm*`; `hires` requires `bit_depth >= 24` or `sample_rate > 48000`, cascading `hires -> cd -> YTM`), full tags (`title`, `artist`, `album`, `track`, `date`, `isrc`), and square cover art (center-crop 16:9 YouTube thumbnails or high-res `cover_url`).
- **Definition of Done (DoD)**:
  - Isolated tests verify lazy short-circuiting in `lib/providers.py`, archive skip on repeat run, `dl sync` prune/mirror, custom template directory creation, `.lrc` sidecar generation, `--format auto` remux without re-encoding, strict `cd`/`hires` `ffprobe` tier rejection & cascade, `small` `libopus`/`m4a` transcode, `--format mp3`/`m4a`/`opus`/`flac` outputs via `ffprobe`, and concurrent `--jobs 2` completion.

### [x] Chunk WP2E: Local Library Indexer & Artist/Album Grouping (`lib/library.py`)
- **Target Files**: `lib/library.py` (new), `bin/musicctl`, `tests/test_tui.py`, `tests/test_musicctl.sh`
- **Steps**:
  1. Implement `lib/library.py` scanning `$MUSICCTL_DOWNLOAD_DIR` incrementally (keyed by `(path, mtime_ns, size)` in `$MUSICCTL_CACHE_DIR/library.json`) using `ffprobe` tags (`artist`, `album`, `title`, `track`, `duration`, `codec`) with filename fallback.
  2. Upgrade `musicctl local [--by flat|artist|album] [--rescan]` for fast grouped listing and local instant search.
- **Definition of Done (DoD)**:
  - Test creates tagged `.flac`/`.m4a` files, verifies incremental cache hit (0 duplicate `ffprobe` calls on unchanged mtime), and verifies `local --by artist` and `local --by album` output.

### [x] Chunk WP2F: Provider Chain v2 (DASH FLAC + OpenSubsonic) & UNIX Lifecycle Hooks
- **Target Files**: `lib/providers.py`, `lib/mpv_hooks.lua`, `bin/musicctl`, `lib/downloader.py`, `examples/providers/README.md`, `examples/providers/10-lossless-template.sh`, `examples/providers/20-opensubsonic.sh` (new), `examples/hooks/README.md` (new), `tests/test_musicctl.sh`
- **Steps**:
  1. Extend `lib/providers.py` stdin JSON contract (backwards-compatible) with `source`, `isrc`, `album`, `track_num`, `year`, `format`; support Orpheon-style `manifest` stdout output (MPEG-DASH `.mpd` / fragmented MP4 FLAC) with `ffmpeg -c copy` DASH-to-FLAC remuxing alongside direct `url`, maintaining lazy multi-candidate fallback if a higher-priority stream/download fails validation.
  2. Add a ready-to-use OpenSubsonic/Navidrome lossless provider script (`examples/providers/20-opensubsonic.sh`) using standard OpenSubsonic auth/token env vars (`MUSICCTL_SUBSONIC_URL`, `MUSICCTL_SUBSONIC_USER`, `MUSICCTL_SUBSONIC_PASS`).
  3. Add non-blocking UNIX lifecycle hooks in `$MUSICCTL_HOOKS_DIR` (`~/.config/musicctl/hooks/`): `on-track-change` (triggered from `lib/mpv_hooks.lua` `file-loaded` event for full CLI + TUI parity) and `on-download-complete`, executed detached with strict timeout (`3s`) and JSON stdin payload, plus documented example adapters for **ListenBrainz / Last.fm scrobbling** and **Discord Rich Presence**.
- **Definition of Done (DoD)**:
  - Mock hook test verifies JSON payload delivery on `on-download-complete` and `on-track-change`, mock DASH `.mpd` / provider test verifies FLAC remux & OpenSubsonic adapter JSON output, and slow/non-zero hooks never fail or block playback/download.

### [x] Chunk WP2G: Non-Blocking TUI Download Manager & JSONL Progress
- **Target Files**: `lib/downloader.py`, `lib/tui.py`, `bin/musicctl`, `tests/test_tui.py`
- **Steps**:
  1. Add `--jsonl` progress output mode to `lib/downloader.py` emitting `{job_id, title, stage, pct, status, dest, error}` events.
  2. Replace the 30s `run_cli_async(["download", ...])` call in `lib/tui.py` with a dedicated background `DownloadManager` feeding `ui_events` (`queue.SimpleQueue`) with live progress toast/status and no 30s timeout kill on albums/playlists.
  3. Add `dl status` / `dl cancel` and TUI right-panel download queue summary so active/failed batch downloads are inspectable without blocking playback.
- **Definition of Done (DoD)**:
  - Unit tests in `test_tui.py` verify JSONL progress event parsing and `drain_ui_events` progress/completion/error state transitions without blocking the curses loop.

### [x] Chunk WP2H: TUI Multi-Catalog Switcher, Drill-Down Stack & `7 Pl` Playlists Tab
- **Target Files**: `lib/tui.py`, `tests/test_tui.py`
- **Steps**:
  1. Add clickable & hotkey catalog switcher (`[Cat:ytm|deezer|itunes]`) and search scope selector (`[Songs] [Albums] [Artists] [Playlists]`) to the search bar in `lib/tui.py`.
  2. Implement navigation stack (`self.nav_stack`) for drill-down browsing across catalogs and local library grouping (`Flat` -> `Artists` -> `Albums`): `Enter` opens child tracks/albums; `[<- Back]` or `Backspace` restores cursor/scroll; expose `Play All`, `+Queue All`, and `Download All` actions plus `[Auto:on/off]` autoplay toggle.
  3. Add `7 Pl` tab listing local named playlists (`$MUSICCTL_DATA_DIR/playlists/*.tsv`) with drill-down into tracks and playlist play/sync triggers.
- **Definition of Done (DoD)**:
  - Unit tests verify `nav_stack` push/pop cursor restoration, catalog/scope switching, `6 Local` grouping toggle, and `7 Pl` TSV loading.

### [x] Chunk WP2I: Packaging, Completions, Docs, Obsidian Sync & `v2.2.0` Verification
- **Target Files**: `install.sh`, `Makefile`, `packaging/arch/PKGBUILD`, `completions/musicctl.bash`, `completions/_musicctl`, `README.md`, `CHANGELOG.md`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Register `lib/catalog.py`, `lib/downloader.py`, `lib/library.py`, `lib/providers.py`, and `lib/mpv_hooks.lua` in `install.sh`, `Makefile` (`py_compile`), and `packaging/arch/PKGBUILD`.
  2. Update bash/zsh completions, `cmd_help`, `README.md`, and `CHANGELOG.md` with multi-catalog search/browse, audiophile quality/SponsorBlock/autoplay, download flags, local grouping, and hooks documentation.
  3. Bump version to `2.2.0`, verify `make lint && make test` exits `0`, and sync `00-AGY-Memory/musicctl` (`INDEX`, `CONTEXT`, `STATE`, `DECISIONS`).
- **Definition of Done (DoD)**:
  - `make lint && make test` exits `0` across all bash + python unit/integration tests; `install.sh --prefix` dry install/uninstall verifies all `lib/*.py` modules are packaged and clean.

---

## 3. Active Work Packet 3 (`v2.3.0`: Adaptive Full-Space Layout, Lightweight Animation & Hybrid Keyboard+Mouse TUI)

- **Track**: Track B / Spec-Driven Incremental Delivery (Zero-Bloat CLI & TUI)
- **Status**: COMPLETED [4/4 CHUNKS]
- **Scope Cuts (Out of Scope / Dropped)**:
  - Removed `cava` FFT daemon, Kitty/Sixel/iTerm2 multi-protocol image engines, multi-theme switcher, `musicctl stats` analytics, and MusicBrainz/AcoustID tagger to preserve the ~20-30 MB footprint and zero-bloat UX.

### [x] Chunk WP3A: Responsive Adaptive Layout Engine (`lib/tui.py`)
- **Target Files**: `lib/tui.py`
- **Steps**:
  1. Replace fixed 52/48 side-by-side split and hardcoded `top_h = min(13, ...)` (which leaves half the right pane/screen empty on tall or portrait terminals) with aspect-aware layout calculation:
     - **Wide/Landscape** (`w >= 110` and wide aspect): proportional ~58/42 split with columnar list formatting and balanced right pane.
     - **Tall/Portrait/Split** (`w < 110` or tall aspect): stacked vertical layout — compact full-width `NOW PLAYING` header card + full-width track list + collapsible bottom drawer (`Up Next` / `Lyrics` / `Info` / `DL`) using 100% terminal width without half-empty void.
  2. Add layout mode cycling (`v`: `auto` -> `split` -> `stacked` -> `focus`) and drawer/side-pane toggle (`z`).
- **Definition of Done (DoD)**:
  - Layout helper returns distinct balanced geometry for wide, tall/portrait, and focus modes without overlapping or clipped borders.

### [x] Chunk WP3B: Lightweight Animation, Art Scaling & Lyrics Race Fix (`lib/tui.py`)
- **Target Files**: `lib/tui.py`
- **Steps**:
  1. Keep animation 100% pure Python stdlib (zero external processes like `cava`), upgrading the hardcoded 28-char wave (`spec_w = min(info_w, 28)`) into a full-width, subtle, low-CPU multi-bar pulse/progress accent matching card width.
  2. Scale `chafa` symbol preview dynamically to available card dimensions instead of hardcoded `22x10`.
  3. Fix the `watch?v=<id>` lyrics race bug: defer `fetch_lyrics_async` while `media-title` is still a raw URL/video-ID placeholder and trigger automatically once `mpv` resolves the real title/artist; render compact contextual track info/quick-hints when Lyrics or Queue is empty so the panel is never an empty black void.
- **Definition of Done (DoD)**:
  - `watch?v=` titles defer lyrics fetch until real metadata resolves, spectrum width adapts to card width, and empty sub-panels show helpful contextual hints.

### [x] Chunk WP3C: Full Hybrid Keyboard + Mouse Parity (`lib/tui.py`)
- **Target Files**: `lib/tui.py`
- **Steps**:
  1. Guarantee every clickable control has a first-class keyboard shortcut and visible compact key hint on buttons (e.g. `[b Prev]`, `[Space Pause]`, `[n Next]`, `[o Loop]`).
  2. Add dual-pane focus navigation (`h`/`l` or `Left`/`Right`) with active focused border highlight; when Right Panel is focused, support `j`/`k`, `Up`/`Down`, `PgUp`/`PgDn`, `Ctrl-U`/`Ctrl-D`, `g`/`G` scrolling across `Up Next`, `Lyrics`, `Info`, and `DL`, plus `Enter` to jump in `Up Next`.
  3. Add fast list paging (`PgUp`/`PgDn`, `Ctrl-U`/`Ctrl-D`, `Home`/`End`) in Left List.
  4. Add keyboard shortcuts for mouse-only actions: `P` (`Play All`), `E` (`+Q All`), `W` (`DL All`), `X` (`Cancel DL` on DL tab), `Ctrl-U`/`Ctrl-K` (clear search), `Shift-Tab` (reverse right-tab cycle), plus direct right-tab keys (`1-4` when right pane focused, or `U`/`Y`/`I`/`D`).
  5. Update `?` Help modal to show unified hybrid Keyboard + Mouse cheat sheet.
- **Definition of Done (DoD)**:
  - All clickable controls have matching keyboard triggers, pane focus toggles between left/right panes, and right-pane content scrolls cleanly via keyboard.

### [x] Chunk WP3D: Tests, Docs & Verification (`tests/test_tui.py`, `README.md`)
- **Target Files**: `tests/test_tui.py`, `README.md`
- **Steps**:
  1. Add unit tests in `tests/test_tui.py` for adaptive layout geometry (`split` vs `stacked` vs `focus`), pane focus switching & right-pane scrolling, new keyboard shortcuts (`P`, `E`, `W`, `X`, `v`, `z`, `PgUp`/`PgDn`), and `watch?v=` lyrics deferral/re-trigger.
  2. Update `README.md` keybindings table and verify `make lint && make test-offline` exits `0`.
- **Definition of Done (DoD)**:
  - `make lint && make test-offline` exits `0` with full unit coverage for WP3A-WP3C.
