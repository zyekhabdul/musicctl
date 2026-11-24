# Changelog

## v1.0.0 (2026-11-24)

- Standalone Bash + `fzf` + `mpv` IPC + `yt-dlp` YouTube Music CLI & TUI player.
- Ad-free InnerTube (`WEB_REMIX`) search & Up Next Radio (`RDAMVM`) mix queuing.
- Background `mpv` JSON IPC daemon with `play`, `add`, `toggle`, `next`, `prev`, `jump`, `remove`, `clear`, `shuffle`, `loop`, `mute`, `speed`, `vol`, `seek`, `queue`, and `status [--json]`.
- Named local playlists (`pl`), favorites (`fav`), playback history, offline Opus download (`download`), and local file playback (`local`).
- LRCLIB lyrics lookup with `--live` real-time synced lyrics watcher.
- Session persistence (`save` / `resume`, auto-saved on `stop`), optional auto-caching (`MUSICCTL_CACHE=1`, `cache status|clear`), `chafa` terminal album art preview, MPRIS auto-detection, sleep timer, desktop notifications, and `doctor` diagnostics.
- Bash/Zsh completions, FreeDesktop entry, turnkey `install.sh`, `Makefile`, and Arch `PKGBUILD`.
