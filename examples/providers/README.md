# Pluggable Audio Providers for musicctl

`musicctl` follows a lightweight UNIX provider chain (inspired by Orpheon's ordered `audio-source` / `download-source` model without a JS sandbox or bundled credentials).

## How It Works

Place executable scripts in `~/.config/musicctl/providers/` (or `$MUSICCTL_PROVIDERS_DIR`). They are executed in lexical filename order (`10-...`, `20-...`, ..., `90-...`) whenever streaming or downloading tracks.

If every provider returns empty output or fails validation, `musicctl` automatically falls back to YouTube Music (`yt-dlp` `--ignore-config`).

## Provider JSON Contract

**Stdin (from `musicctl`, v2 contract):**
```json
{
  "id": "IR7tYHdYN2Q",
  "source": "ytm",
  "title": "To the Bone",
  "artist": "Pamungkas",
  "album": "Walk the Talk",
  "isrc": "ID1234567890",
  "track_num": 4,
  "year": 2018,
  "duration": 345,
  "quality": "cd",
  "format": "auto",
  "mode": "download"
}
```
- `source`: `ytm`, `deezer`, `itunes`, `spotify`, or `local`.
- `quality`: `small`, `cd` (FLAC 16-bit/44.1kHz target), or `hires` (FLAC 24-bit/96-192kHz target).
- `format`: requested container (`auto`, `flac`, `opus`, `m4a`, `mp3`).
- `mode`: `stream` or `download`.

**Stdout (from provider):**
Return empty output (and exit 0 or non-zero) to pass to the next provider, or output a single JSON object with either `url` or MPEG-DASH `manifest`:
```json
{
  "url": "https://cdn.example.com/track.flac",
  "codec": "flac",
  "bit_depth": 16,
  "sample_rate": 44100,
  "headers": {
    "Authorization": "Bearer ..."
  }
}
```
Or an Orpheon-style MPEG-DASH FLAC manifest (HTTP(S) URL, local `.mpd` path, `file://`, or inline `<MPD ...>` XML):
```json
{
  "manifest": "https://cdn.example.com/stream.mpd",
  "codec": "flac",
  "bit_depth": 24,
  "sample_rate": 96000
}
```
Local paths (`/path/to/file.flac` or `file://...`) are also accepted. Included OpenSubsonic/Navidrome adapter: [20-opensubsonic.sh](20-opensubsonic.sh).

## Verification & Auto-Tagging

For downloads, `musicctl` verifies every file returned by a provider using `ffprobe` (real audio codec + duration within `-5s` to `+15s` of metadata duration when known).
- If `quality=small` and the provider returns lossless audio (`FLAC`/`ALAC`/`WAV`), `musicctl` transcodes locally to `128kbps` `.opus` via `libopus` by default (or `128kbps` `.m4a` when `--format m4a` is passed).
- Title/artist metadata and cover artwork (for `.flac`, `.m4a`, `.mp3`) are automatically embedded via `ffmpeg`.
