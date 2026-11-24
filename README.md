# musicctl

Lightweight, standalone YouTube Music CLI & TUI player modeled after `gamectl`.
Streams ad-free audio via InnerTube (`WEB_REMIX`) + `yt-dlp` + background `mpv` IPC daemon + `fzf` cockpit (~20-30 MB RAM, zero Electron).

```text
╭─ NOW PLAYING ─────────────────────────────────╮
 [PLAY] To the Bone
 [========>-----------] 02:14/05:45
 Vol: 80%  Queue: 1/12  Loop: off
╰───────────────────────────────────────────────╯
╭─ SELECTED TRACK ──────────────────────────────╮
 Title : To the Bone
 Info  : Pamungkas • Flying Solo • 5:45
 ID    : IR7tYHdYN2Q [YTM]
 Fav   : Yes
╰───────────────────────────────────────────────╯
```

## Quick Install

```bash
# User install (~/.local/bin + bash/zsh completions + desktop entry)
./install.sh

# Or via Makefile
make lint && make test && make install
```

Arch Linux PKGBUILD is provided in [packaging/arch/PKGBUILD](packaging/arch/PKGBUILD).

## Dependencies

- **Core**: `bash`, `mpv`, `yt-dlp`, `fzf`, `curl`, `jq`, `python3`
- **Optional**:
  - `chafa`: terminal album art preview inside TUI cockpit
  - `notify-send` (`libnotify`): track change desktop notifications
  - `mpv-mpris`: native MPRIS media key & lockscreen integration (`playerctl`)

Run `musicctl doctor` to verify dependencies, MPRIS detection, daemon state, and cache usage.

## CLI Usage

```bash
musicctl                        # Launch interactive fzf Sovereign Cockpit
musicctl tui "pamungkas"        # Launch TUI preloaded with YTM search
musicctl play "to the bone"     # Search YTM (or local file/URL/ID) and play
musicctl add "one only"         # Append top match to mpv queue
musicctl search "lofi beats"    # Print TSV search results (id, title, meta)
musicctl radio [current|query]  # Start/append YTM Up Next Radio mix
musicctl toggle                 # Toggle play/pause
musicctl next | prev            # Next / previous track in queue
musicctl jump <N>               # Jump to queue item #N (1-based)
musicctl remove <N>             # Remove queue item #N
musicctl clear [--all]          # Clear upcoming queue (or --all incl. current)
musicctl shuffle                # Shuffle active mpv queue
musicctl loop [off|one|all]     # Cycle or set repeat mode
musicctl mute                   # Toggle mute
musicctl eq [preset|cycle]      # Set EQ (flat/bass/warm/vocal/bright/rock)
musicctl norm [on|off|toggle]   # Toggle EBU R128 loudness normalization
musicctl speed <0.25..4.0|reset># Set playback speed
musicctl vol 80                 # Set volume (supports +10 / -10)
musicctl seek +15               # Seek relative seconds
musicctl status [--json]        # Show status, loop/mute/speed, or Waybar JSON
musicctl queue                  # List active mpv queue
musicctl lyrics [--live] [q]    # Fetch lyrics via LRCLIB (or --live sync)
musicctl download [current|q]   # Download track as Opus audio + metadata
musicctl local                  # List downloaded local audio files
musicctl pl ls|create|add|show|play|rm|delete  # Named local playlists
musicctl sleep <min|off|status> # Auto-stop sleep timer
musicctl resume | save          # Restore or snapshot queue & playback position
musicctl cache [status|clear]   # Inspect or clear auto-cached audio & art
musicctl fav list|add|rm|play   # Manage favorites
musicctl history                # Show recent playback history
musicctl doctor                 # Inspect environment and optional integrations
musicctl stop                   # Save session and stop background mpv daemon
```

## TUI Keybindings

| Key | Action |
| --- | --- |
| `Enter` | Play selected track (or jump if viewing Queue) |
| `Ctrl-A` | Append selected track to queue |
| `Ctrl-S` | Live search YouTube Music for current query |
| `Ctrl-R` | Queue YTM Radio mix from selected track |
| `Ctrl-F` | Toggle favorite for selected track |
| `Ctrl-L` | View Favorites |
| `Ctrl-H` | View History |
| `Ctrl-Q` | View Active Queue |
| `Ctrl-D` | View Downloaded Local Audio |
| `Ctrl-P` | Toggle play/pause |
| `Ctrl-N` | Next track |
| `Ctrl-B` | Previous track |
| `Ctrl-O` | Cycle loop mode (`off` / `one` / `all`) |
| `Ctrl-U` | Shuffle queue |
| `Ctrl-X` | Save session and stop `mpv` daemon |
| `Alt-L` | View plain lyrics (`less -R`) |
| `Alt-K` | Launch live synced lyrics watcher |
| `Alt-M` | Toggle mute |
| `Alt-E` | Cycle EQ preset (`flat`/`bass`/`warm`/`vocal`/`bright`/`rock`) |
| `Alt-N` | Toggle EBU R128 loudness normalization |
| `Alt-=` / `Alt--` | Volume `+5%` / `-5%` |
| `Alt-Right` / `Alt-Left` | Seek `+10s` / `-10s` |

## Configuration (`~/.config/musicctl/config`)

Optional `KEY=VALUE` config file (explicit environment variables always override):

```ini
MUSICCTL_CACHE=1
MUSICCTL_NOTIFY=1
MUSICCTL_ART=1
MUSICCTL_ART_FORMAT=symbols
MUSICCTL_EQ=flat
MUSICCTL_NORM=0
MUSICCTL_DOWNLOAD_DIR="$HOME/Music/musicctl"
```

Set `MUSICCTL_ART_FORMAT=sixel` for high-resolution Sixel album art on terminals like `foot`.

## Waybar Custom Module Snippet

```jsonc
"custom/musicctl": {
  "exec": "musicctl status --json",
  "return-type": "json",
  "interval": 2,
  "format": "{}",
  "on-click": "musicctl toggle",
  "on-scroll-up": "musicctl vol +5",
  "on-scroll-down": "musicctl vol -5"
}
```

## License

[MIT](LICENSE)
