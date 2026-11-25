# WORK PACKET 1: v2.0.0 CHECKPOINT & v2.1.0 MYSPOTI-INSPIRED ENHANCEMENTS

- **Track**: Track B / Spec-Driven Incremental Delivery (Zero-Bloat CLI & TUI)
- **Status**: COMPLETED [6/6 CHUNKS]
- **Obsidian Namespace**: `00-AGY-Memory/musicctl` (`INDEX`, `CONTEXT`, `STATE`, `DECISIONS`)
- **Base Commit**: `6735678463faab80f364ced6fd59d35269710e10` (`v1.1.0`)
- **Verified Baseline**: `make lint` exit 0 | `test_musicctl.sh` 117/117 PASS | `test_tui.py` 12/12 OK

---

## 1. Objectives & Architectural Boundaries

1. **Preserve Lightweight Footprint**: Keep runtime ~20-30 MB RAM using native Bash + Python 3 stdlib + `mpv` IPC + `yt-dlp`. Strictly reject hidden WebView/browser engines or third-party TUI frameworks (per `ADR-002`, `ADR-006`).
2. **Adopt High-Value Myspoti Workflows**: Bring queue/playlist reordering, offline playlist synchronization, multi-provider lyric fallback, cross-platform Spotify playlist URL import, and 3-tier audio quality (`small` / `cd` / `hires`) via a pluggable UNIX provider chain into `musicctl`.

---

## 2. Active Work Packet (Max 10 Chunks)

### [x] Chunk WP1A: Checkpoint Verified `v2.0.0` Dashboard & History Self-Healing
- **Target Files**: `bin/musicctl`, `lib/tui.py`, `install.sh`, `Makefile`, `packaging/arch/PKGBUILD`, `completions/*`, `README.md`, `CHANGELOG.md`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Unlock GPG key in `gpg-agent` (`2976A87B6EBAF1E5`) and commit source/packaging changes atomically.
  2. Commit test additions/updates separately (`ALLOW_TEST_MUTATION=1`).
- **Definition of Done (DoD)**:
  - `git status -s` clean on `main`; `make lint && make test` exits `0`.

### [x] Chunk WP1B: Queue & Named Playlist Track Reordering
- **Target Files**: `bin/musicctl`, `lib/tui.py`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Add `musicctl move <from_idx> <to_idx>` using `mpv` IPC `["playlist-move", from, to]`.
  2. Add `musicctl pl move <name> <from_idx> <to_idx>` to reorder rows atomically in `<name>.tsv`.
  3. Add TUI hotkeys (`J`/`K` on `Queue` tab) and clickable `[Up]` / `[Down]` buttons in `Right -> Info`/`Up Next`.
- **Definition of Done (DoD)**:
  - Isolated `mpv --ao=null` test verifies `playlist-move` order change; `pl move` test verifies TSV row swap.

### [x] Chunk WP1C: Offline Playlist Smart-Sync (`musicctl pl sync`)
- **Target Files**: `bin/musicctl`, `completions/musicctl.bash`, `completions/_musicctl`, `tests/test_musicctl.sh`
- **Steps**:
  1. Implement `musicctl pl sync <name>`: iterates `<name>.tsv`, skips tracks already present via `find_cached_audio`, and downloads missing YouTube IDs into `$CACHE_AUDIO_DIR` as Opus 48kHz (`--ignore-config -f 251/bestaudio...`).
  2. Add `--prune` option or `pl play <name>` auto-hit via `resolve_play_target`.
- **Definition of Done (DoD)**:
  - Dry/isolated test proves cached tracks are skipped (0 duplicate fetches) and missing targets are resolved cleanly.

### [x] Chunk WP1D: Multi-Provider Lyrics Fallback (LRCLIB -> Secondary Plain Scraper)
- **Target Files**: `bin/musicctl`, `lib/tui.py`, `tests/test_tui.py`
- **Steps**:
  1. When LRCLIB `/api/get` and `/api/search` (`pick_best_lyrics_candidate`) return `None`, query a lightweight secondary plain-lyrics HTTP API (e.g., `lyrics.ovh` `https://api.lyrics.ovh/v1/<artist>/<title>`) with strict 3s timeout.
  2. Mark source cleanly in header (`[LRCLIB]` vs `[Fallback]`) without blocking playback or UI thread.
- **Definition of Done (DoD)**:
  - Offline mock/parser unit test verifies fallback parsing and clean failure when both providers return empty.

### [x] Chunk WP1E: Public Spotify Track/Playlist URL Importer (`musicctl pl import`)
- **Target Files**: `bin/musicctl`, `lib/tui.py`, `README.md`, `tests/test_musicctl.sh`
- **Steps**:
  1. Parse public Spotify playlist/album/track metadata (HTML OpenGraph / embedded JSON `<script id="__NEXT_DATA__">` or Spotify oEmbed for single tracks — zero API keys/login required).
  2. Resolve each `"artist title"` via `ytm_search | head -n 1` and write into a named playlist `<name>.tsv` (or enqueue via `musicctl play <spotify_url>`).
- **Definition of Done (DoD)**:
  - Offline HTML fixture test extracts track list accurately; `make lint && make test` exits `0`.

### [x] Chunk WP1F: 3-Tier Audio Quality & Pluggable Lossless Provider Chain (Orpheon-Style)
- **Target Files**: `bin/musicctl`, `lib/tui.py`, `examples/providers/README.md`, `examples/providers/10-lossless-template.sh`, `completions/musicctl.bash`, `completions/_musicctl`, `README.md`, `tests/test_musicctl.sh`, `tests/test_tui.py`
- **Steps**:
  1. Add 3 quality tiers (`small` = low-bitrate lossy ~50-128k or local `ffmpeg` AAC/Opus transcode from FLAC; `cd` = default FLAC 16-bit/44.1kHz -> fallback YTM `251` Opus 48kHz; `hires` = FLAC 24-bit/96-192kHz -> fallback `cd` -> fallback YTM `251`) plus `ask` mode for interactive downloads. Add `musicctl quality [small|cd|hires|ask]`, `MUSICCTL_QUALITY` config key, and `dl --quality <tier>`.
  2. Implement ordered UNIX pluggable provider chain in `~/.config/musicctl/providers/` (lexically ordered executable scripts, stdin JSON `{title, artist, album, duration, quality, mode}`, stdout JSON `{url, codec, bit_depth, sample_rate, headers}` or empty for fallback; zero embedded credentials or JS sandbox; built-in YTM remains default fallback).
  3. Add `ffprobe` download/cache integrity validator: verify actual stream codec, `bit_depth`, `sample_rate`, and duration guard (`-5s` to `+15s` vs track metadata) before accepting downloaded files; reject mismatches and fall through to next provider. Support local `ffmpeg` transcode when tier is `small` and provider returns FLAC, and auto-embed metadata tags (`title`/`artist`/`album`) + cover art (`ffmpeg` attached pic for `.flac`/`.m4a`/`.mp3`, metadata tags for `.opus`, and `--embed-metadata --embed-thumbnail` for YTM `yt-dlp`).
  4. Surface actual codec/bit-depth/sample-rate badge (`FLAC 24-bit 96kHz` / `OPUS 48kHz`) and clickable `[Q:cd]` quality switcher in `lib/tui.py` + provide documented example provider template in `examples/providers/`.
- **Definition of Done (DoD)**:
  - Isolated tests with mock provider script verify priority order, JSON contract, fallback to YTM on empty/failed provider, `ffprobe` duration-mismatch rejection, `small` transcode path, embedded metadata/cover tags via `ffprobe`, and TUI quality badge formatting; `make lint && make test` exits `0`.
