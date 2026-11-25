#!/usr/bin/env bash
# Example ordered lossless provider for musicctl.
# Copy to ~/.config/musicctl/providers/10-lossless.sh and chmod +x.
#
# Reads v2 JSON request on stdin:
#   {"id":"...","source":"ytm","title":"...","artist":"...","album":"...","isrc":"...","track_num":1,"year":2024,"duration":345,"quality":"cd","format":"auto","mode":"download"}
# Outputs JSON {"url":"..." | "manifest":"...","codec":"flac","bit_depth":16,"sample_rate":44100,"headers":{}} or empty to fall through.

set -euo pipefail
REQ="$(cat)"
TITLE="$(jq -r '.title // ""' <<<"$REQ")"
ARTIST="$(jq -r '.artist // ""' <<<"$REQ")"
ALBUM="$(jq -r '.album // ""' <<<"$REQ")"
ISRC="$(jq -r '.isrc // ""' <<<"$REQ")"
SOURCE="$(jq -r '.source // "ytm"' <<<"$REQ")"
QUALITY="$(jq -r '.quality // "cd"' <<<"$REQ")"
FORMAT="$(jq -r '.format // "auto"' <<<"$REQ")"

# Example configuration: point to an authorized local Navidrome/Subsonic or custom lossless bridge.
# Leave MUSICCTL_LOSSLESS_BRIDGE unset to cleanly fall through to built-in YouTube Music.
BRIDGE_URL="${MUSICCTL_LOSSLESS_BRIDGE:-}"
[[ -n "$BRIDGE_URL" ]] || exit 0

exec curl -fsSL -m 6 -G "$BRIDGE_URL/resolve" \
  --data-urlencode "source=$SOURCE" \
  --data-urlencode "title=$TITLE" \
  --data-urlencode "artist=$ARTIST" \
  --data-urlencode "album=$ALBUM" \
  --data-urlencode "isrc=$ISRC" \
  --data-urlencode "quality=$QUALITY" \
  --data-urlencode "format=$FORMAT"
