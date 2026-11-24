# Changelog

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
