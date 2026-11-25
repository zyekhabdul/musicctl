# Changelog

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
