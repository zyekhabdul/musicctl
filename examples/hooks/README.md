# UNIX Lifecycle Hooks for musicctl

`musicctl` emits non-blocking JSON lifecycle events to executable scripts in `~/.config/musicctl/hooks/` (override via `$MUSICCTL_HOOKS_DIR`).

## Hook Discovery & Execution Guarantees

- Direct hook executable: `$MUSICCTL_HOOKS_DIR/<event>` (for example `~/.config/musicctl/hooks/on-track-change`)
- Drop-in directory scripts: `$MUSICCTL_HOOKS_DIR/<event>.d/*` (executed in lexical order)
- Every hook receives a single JSON object on **stdin**. `on-track-change` runs detached via `mpv` (`command_native_async`), while `on-download-complete` runs per downloaded track with a strict **3-second timeout** and ignored errors so slow or failing hooks never fail playback or downloads.

## Events

### `on-track-change`

Triggered by `mpv` on `file-loaded` (full CLI + TUI parity).

```json
{
  "event": "on-track-change",
  "id": "IR7tYHdYN2Q",
  "path": "https://www.youtube.com/watch?v=IR7tYHdYN2Q",
  "title": "To the Bone",
  "artist": "Pamungkas",
  "album": "Walk the Talk",
  "duration": 345.0,
  "timestamp": 1764060000
}
```

### `on-download-complete`

Triggered by `lib/downloader.py` when a track finishes downloading and tagging.

```json
{
  "event": "on-download-complete",
  "id": "IR7tYHdYN2Q",
  "source": "ytm",
  "title": "To the Bone",
  "artist": "Pamungkas",
  "album": "Walk the Talk",
  "isrc": "ID1234567890",
  "quality": "cd",
  "format": "auto",
  "codec": "flac",
  "path": "/home/user/Music/musicctl/Pamungkas/Walk the Talk/04 - To the Bone.flac",
  "timestamp": 1764060012
}
```

## Example Adapters

### ListenBrainz Scrobbler (`~/.config/musicctl/hooks/on-track-change.d/10-listenbrainz.sh`)

```bash
#!/usr/bin/env bash
set -euo pipefail
[[ -n "${LISTENBRAINZ_TOKEN:-}" ]] || exit 0
REQ="$(cat)"
TITLE="$(jq -r '.title // ""' <<<"$REQ")"
ARTIST="$(jq -r '.artist // ""' <<<"$REQ")"
ALBUM="$(jq -r '.album // ""' <<<"$REQ")"
[[ -n "$TITLE" && -n "$ARTIST" ]] || exit 0

PAYLOAD="$(jq -cn --arg t "$TITLE" --arg a "$ARTIST" --arg b "$ALBUM" '{
  listen_type: "playing_now",
  payload: [{track_metadata: {artist_name: $a, track_name: $t, release_name: $b}}]
}')"

curl -fsSL -m 2 -X POST "https://api.listenbrainz.org/1/submit-listens" \
  -H "Authorization: Token ${LISTENBRAINZ_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" >/dev/null || true
```

### Discord Rich Presence IPC (`~/.config/musicctl/hooks/on-track-change.d/20-discord-rpc.py`)

```python
#!/usr/bin/env python3
"""Forward track updates to a local Discord RPC socket or bridge."""
import json
import os
import sys

payload = json.load(sys.stdin)
sock_path = os.environ.get("DISCORD_IPC_BRIDGE", "")
if sock_path and payload.get("title"):
    with open(os.devnull, "w", encoding="utf-8"):
        pass  # Send formatted activity frame to your local RPC bridge.
```
