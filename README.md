# musicctl

Lightweight, standalone YouTube Music CLI & TUI player modeled after `gamectl`.
Streams ad-free audio via InnerTube (`WEB_REMIX`) + `yt-dlp` + background `mpv` IPC daemon + **clickable realtime Sovereign Dashboard** (~20-30 MB RAM, zero Electron).

```text
 MUSICCTL v2.0   1 Home  2 Search  3 Queue  4 Favs  5 Hist  6 Local         [PLAY] OPUS 48kHz
 Search: [ pamungkas to the bone        ]  [Search] [Clear]
╭─ 1 Home (12) ─────────────────────────╮╭─ NOW PLAYING ──────────────────────────────────────╮
│▸ 1.* To the Bone — Pamungkas    [FAV] ││ [ART]  To the Bone                                │
│  2.  Monolog — Pamungkas        [HIS] ││        Audio: opus 48kHz  |  Queue: 1/12           │
│  3.  One Only — Pamungkas       [HIS] ││        EQ: flat  |  Norm: off  |  Loop: off       │
│                                       ││        ▂▅▇▃▆▄▂▅▇▆▃▂▅▇▄▃▆▂▅▇                        │
│                                       │╰────────────────────────────────────────────────────╯
│                                       │╭─ [Up Next] [Lyrics] [Info] ────────────────────────╮
│                                       ││▸  1. To the Bone                                   │
│                                       ││   2. Monolog                                       │
╰───────────────────────────────────────╯╰────────────────────────────────────────────────────╯
 02:14  ━━━━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  05:45
 [|< Prev] [|| Pause] [>| Next] [[] Stop] [~ Radio] [* Fav]   Vol [-] ████████░░  80% [+]
 [Loop:off] [Shuf] [EQ:flat] [Norm:off] [Mute] [? Help]
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
  - `ffmpeg` (`ffprobe`): lossless provider integrity validation, `small` transcode, and metadata/cover embedding
  - `notify-send` (`libnotify`): track change desktop notifications
  - `mpv-mpris`: native MPRIS media key & lockscreen integration (`playerctl`)

Run `musicctl doctor` to verify dependencies, MPRIS detection, daemon state, and cache usage.

## CLI Usage

```bash
musicctl                        # Launch clickable realtime Sovereign Dashboard
musicctl tui "pamungkas"        # Launch Dashboard preloaded with YTM search
musicctl fzf [query]            # Launch legacy fzf cockpit (1s realtime ticker)
musicctl play "to the bone"     # Search YTM (or local file/URL/ID) and play
musicctl add "one only"         # Append top match to mpv queue
musicctl search "lofi beats"    # Print TSV search results (id, title, meta)
musicctl radio [current|query]  # Start/append YTM Up Next Radio mix
musicctl toggle                 # Toggle play/pause
musicctl next | prev            # Next / previous track in queue
musicctl jump <N>               # Jump to queue item #N (1-based)
musicctl move <from> <to>       # Reorder queue item #from to #to (1-based)
musicctl remove <N>             # Remove queue item #N
musicctl clear [--all]          # Clear upcoming queue (or --all incl. current)
musicctl shuffle                # Shuffle active mpv queue
musicctl loop [off|one|all]     # Cycle or set repeat mode
musicctl mute                   # Toggle mute
musicctl quality [small|cd|hires|ask|cycle]  # Set audio quality & provider priority
musicctl eq [preset|cycle]      # Set EQ (flat/bass/warm/vocal/bright/rock)
musicctl norm [on|off|toggle]   # Toggle EBU R128 loudness normalization
musicctl speed <0.25..4.0|reset># Set playback speed
musicctl vol 80                 # Set volume (supports +10 / -10)
musicctl seek +15               # Seek relative seconds
musicctl status [--json]        # Show status, loop/mute/speed, or Waybar JSON
musicctl queue                  # List active mpv queue
musicctl lyrics [--live] [q]    # Fetch lyrics via LRCLIB / Fallback (or --live sync)
musicctl dl [--quality T] [q]   # Download track (FLAC/M4A/Opus) + embedded tags & cover
musicctl local                  # List downloaded local audio files
musicctl pl ls|create|add|show|play|move|sync|import|rm|delete  # Playlists, smart sync & Spotify URL import
musicctl sleep <min|off|status> # Auto-stop sleep timer
musicctl resume | save          # Restore or snapshot queue & playback position
musicctl cache [status|clear]   # Inspect or clear auto-cached audio & art
musicctl fav list|add|rm|play   # Manage favorites
musicctl history                # Show recent playback history
musicctl doctor                 # Inspect environment and optional integrations
musicctl stop                   # Save session and stop background mpv daemon
```

## TUI Keybindings

### Sovereign Dashboard (Default TUI)

| Control | Action |
| --- | --- |
| Click / Double-click / Right-click row | Select / Play or Jump / Enqueue track |
| Click progress / volume bar, Wheel | Seek timestamp / Set volume / Scroll list |
| `/` or `Ctrl-S` | Focus search input (`Esc` to leave) |
| `1`-`6` / `Tab` | Switch left tabs / Cycle right panel (`Up Next`, `Lyrics`, `Info`) |
| `j`/`k` or `Up`/`Down`, `g`/`G` | Move cursor (`g` top, `G` bottom) |
| `J` / `K` (on Queue tab) | Move selected queue track down / up |
| `Enter` / `a` / `f` / `r` / `d` | Play or Jump / Enqueue / Toggle fav / Queue radio / Download |
| `Space` / `n` / `b` / `x` | Pause or Play / Next / Prev / Stop daemon |
| `o` / `u` / `Q` / `e` / `N` / `m` | Cycle loop / Shuffle / Cycle quality / Cycle EQ / Toggle norm / Mute |
| `+` or `=` / `-`, `[` / `]` | Volume `+5%` / `-5%`, Seek `-10s` / `+10s` |
| `?` / `q` | Toggle help modal / Quit TUI (daemon stays alive) |

### Legacy fzf Cockpit (`musicctl fzf`)

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
MUSICCTL_QUALITY=cd
MUSICCTL_PROVIDERS_DIR="$HOME/.config/musicctl/providers"
MUSICCTL_DOWNLOAD_DIR="$HOME/Music/musicctl"
```

Set `MUSICCTL_ART_FORMAT=sixel` for high-resolution Sixel album art on terminals like `foot`.

For Orpheon-style lossless (`cd`/`hires`) streaming and downloads, place executable provider scripts in `$MUSICCTL_PROVIDERS_DIR` — see [examples/providers/README.md](examples/providers/README.md).

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
