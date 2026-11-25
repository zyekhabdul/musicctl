# Changelog

## v2.2.0 (2026-11-25)

- **Multi-Catalog Entity Engine (`lib/catalog.py`)**: Zero-key search and browse across `ytm`, `deezer` (ISRC + 1000x1000 square art), and `itunes` (`songs|albums|artists|playlists`), plus universal `play`/`add`/`dl` support for catalog refs, Spotify, SoundCloud, Bandcamp, and local files.
- **Metadata & Clean Matching**: End-to-end `album`, `year`, `track_num`, and `isrc` propagation into `mpv`, provider stdin contracts, `ffprobe`/`ffmpeg` tags, `status --json`, and `lib/tui.py` cards, paired with clean title normalization.
- **Audiophile Playback, SponsorBlock & Autoplay (`lib/mpv_hooks.lua`)**: Upgraded YTM format priority (`774/251/141`), opt-in `MUSICCTL_YTM_COOKIES`, `soxr` high-precision resampler, native Hi-Res sample-rate preservation when `norm=off`, `MUSICCTL_REPLAYGAIN`, SponsorBlock auto-skip (`MUSICCTL_SPONSORBLOCK=1`), and CLI + TUI infinite Up Next Radio (`musicctl autoplay`).
- **Production Download Engine (`lib/downloader.py`) & Lazy Provider SSOT (`lib/providers.py`)**: Concurrent `--jobs 1..4` downloads, `--format auto|flac|opus|m4a|mp3`, zero generation-loss remux on `auto`, `libopus` `small` default, `--template`, `--lrc`, `--replaygain`, `--force`, `--dry-run`, `dl sync <source> [--prune]`, `download-archive.tsv`, center-cropped square cover art, `--jsonl` progress, and `dl status|cancel`.
- **Incremental Local Library Indexer (`lib/library.py`)**: `(path, mtime_ns, size)` index at `$MUSICCTL_CACHE_DIR/library.json` powering `musicctl local [--by flat|artist|album] [--rescan] [--json] <query>`.
- **Provider Chain v2 & Lifecycle Hooks**: Added `source`/`format` provider fields, MPEG-DASH `.mpd` FLAC remuxing, OpenSubsonic/Navidrome adapter ([examples/providers/20-opensubsonic.sh](examples/providers/20-opensubsonic.sh)), and 3s non-blocking `on-track-change` / `on-download-complete` UNIX hooks ([examples/hooks/README.md](examples/hooks/README.md)).
- **TUI Drill-Down Dashboard (`lib/tui.py`)**: Added catalog (`[Cat:...]`) & scope (`[Songs|Albums|Artists|Lists]`) switchers, recursive `nav_stack` drill-down with `Play All`/`+Q All`/`DL All`, `6 Local` grouping toggle (`Flat`/`Artists`/`Albums`), `7 Pl` playlists tab, and non-blocking `DownloadManager` (`DL` tab).

## v2.1.0 (2026-11-25)

- Added queue and named playlist track reordering (`musicctl move`, `musicctl pl move`, and TUI `J`/`K` + `Move Up`/`Move Down` buttons).
- Added offline playlist smart-sync (`musicctl pl sync [--quality <tier>] [--prune]`).
- Added multi-provider lyrics fallback (`LRCLIB` -> `lyrics.ovh` `[Fallback]` with 3s timeout).
- Added public Spotify playlist, album, and track URL importer (`musicctl pl import` and `musicctl play <spotify_track_url>`).
- Added 3-tier audio quality (`small` / `cd` / `hires` / `ask`), ordered UNIX pluggable provider chain (`$MUSICCTL_PROVIDERS_DIR`), `ffprobe` audio integrity/duration validation, `ffmpeg` small-tier transcode & metadata/cover embedding, and lossless codec/bit-depth badges in `lib/tui.py`.
- Hardened TUI worker concurrency (`queue.SimpleQueue` main-loop dispatch + stale `track_key`/`search_seq` guards), Bash array quoting in `embed_metadata_cover` fallback, `cmd_tui`/`cmd_sleep` env forwarding, and portable `SCRIPT_DIR`/`ROOT_DIR` paths in `tests/test_musicctl.sh`.
- Consolidated shared Python `mpv` IPC, artist extraction, and lyrics parsing helpers into `lib/common.py`, and replaced `format_mmss`/`ascii_bar` Python spawns with pure-Bash arithmetic.
- Improved streaming resilience and TUI ergonomics: escaped mpv `http-header-fields`, preflight provider stream probing with YTM fallback, all-lossless `small` transcoding, direct Spotify playlist/album `play`/`add`, UTF-8 search input via `get_wch()`, and album art color-pair recycling.

## v2.0.0 (2026-11-25)

- Added native, 100% clickable & realtime Python 3 `curses` Sovereign Dashboard (`lib/tui.py`) as default TUI (`musicctl` / `musicctl tui`):
  - Clickable top navigation tabs (`Home`, `Search`, `Queue`, `Favs`, `Hist`, `Local`), search input box, track rows (single-click select, double-click play/jump, right-click enqueue, wheel scroll), seekable progress bar, transport buttons, clickable volume bar, and DSP toggle pills (`Loop`, `Shuf`, `EQ`, `Norm`, `Mute`).
  - Realtime 250ms loop over single-socket batched `mpv` IPC (~1ms), 256-color half-block `chafa` album art, animated spectrum bar, and live synced LRCLIB lyrics tab.
- Replaced sequential per-property `mpv_get` calls in `status` and `_preview` with single-socket `mpv_batch_status_json` (~1ms IPC roundtrip).
- Fixed `chafa` 5s terminal-probe stall by passing `--probe=off` and fixed `Speed: 1.000000x` leak when speed is `1.0`.
- Upgraded legacy `musicctl fzf` mode with `fzf --listen` 1-second realtime preview ticker and mouse bindings.

## v1.1.0 (2026-11-24)

- Fixed global `~/.config/yt-dlp/config` cookie interference (`--ignore-config`) that downgraded streams to MP4 format `18` (`44.1 kHz` AAC); now streams native YouTube Opus `48 kHz` (`itag 251`).
- Enabled gapless audio, ReplayGain track normalization, and pitch correction on `mpv` daemon.
- Added live parametric EQ presets (`musicctl eq flat|bass|warm|vocal|bright|rock|cycle`) and EBU R128 loudness normalization (`musicctl norm on|off|toggle`) with TUI hotkeys `Alt-E` and `Alt-N`.
- Exposed live `codec`, `samplerate`, `eq`, and `norm` fields in `musicctl status [--json]` and TUI preview.

## v1.0.0 (2026-11-24)

- Standalone Bash + `fzf` + `mpv` IPC + `yt-dlp` YouTube Music CLI & TUI player.
- Ad-free InnerTube (`WEB_REMIX`) search & Up Next Radio (`RDAMVM`) mix queuing.
- Background `mpv` JSON IPC daemon with `play`, `add`, `toggle`, `next`, `prev`, `jump`, `remove`, `clear`, `shuffle`, `loop`, `mute`, `speed`, `vol`, `seek`, `queue`, and `status [--json]`.
- Named local playlists (`pl`), favorites (`fav`), playback history, offline Opus download (`download`), and local file playback (`local`).
- LRCLIB lyrics lookup with `--live` real-time synced lyrics watcher.
- Session persistence (`save` / `resume`, auto-saved on `stop`), optional auto-caching (`MUSICCTL_CACHE=1`, `cache status|clear`), `chafa` terminal album art preview, MPRIS auto-detection, sleep timer, desktop notifications, and `doctor` diagnostics.
- Bash/Zsh completions, FreeDesktop entry, turnkey `install.sh`, `Makefile`, and Arch `PKGBUILD`.
