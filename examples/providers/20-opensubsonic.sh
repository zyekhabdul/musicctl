#!/usr/bin/env bash
# OpenSubsonic / Navidrome lossless audio provider for musicctl.
# Copy to ~/.config/musicctl/providers/20-opensubsonic.sh and chmod +x.
# Required env:
#   MUSICCTL_SUBSONIC_URL="https://music.example.com"
#   MUSICCTL_SUBSONIC_USER="alice"
#   MUSICCTL_SUBSONIC_PASS="secret"

set -euo pipefail

REQ="$(cat)"
BASE_URL="${MUSICCTL_SUBSONIC_URL:-}"
USER_NAME="${MUSICCTL_SUBSONIC_USER:-}"
USER_PASS="${MUSICCTL_SUBSONIC_PASS:-}"
[[ -n "$BASE_URL" && -n "$USER_NAME" && -n "$USER_PASS" ]] || exit 0
BASE_URL="${BASE_URL%/}"

TITLE="$(jq -r '.title // ""' <<<"$REQ")"
ARTIST="$(jq -r '.artist // ""' <<<"$REQ")"
QUALITY="$(jq -r '.quality // "cd"' <<<"$REQ")"
[[ -n "$TITLE" ]] || exit 0

QUERY="${ARTIST:+$ARTIST }$TITLE"
SALT="$(printf '%04x%04x%04x' "$RANDOM" "$$" "$RANDOM")"
TOKEN="$(printf '%s%s' "$USER_PASS" "$SALT" | md5sum | awk '{print $1}')"

SEARCH_JSON="$(curl -fsSL -m 6 -G "${BASE_URL}/rest/search3.view" \
  --data-urlencode "u=$USER_NAME" \
  --data-urlencode "t=$TOKEN" \
  --data-urlencode "s=$SALT" \
  --data-urlencode "v=1.16.1" \
  --data-urlencode "c=musicctl" \
  --data-urlencode "f=json" \
  --data-urlencode "songCount=5" \
  --data-urlencode "albumCount=0" \
  --data-urlencode "artistCount=0" \
  --data-urlencode "query=$QUERY" 2>/dev/null || true)"
[[ -n "$SEARCH_JSON" ]] || exit 0

SONG_JSON="$(jq -ce --arg title "$TITLE" --arg artist "$ARTIST" '
  (.["subsonic-response"].searchResult3.song // []) as $songs
  | ($songs | map(select((.title // "" | ascii_downcase) == ($title | ascii_downcase) and ($artist == "" or (.artist // "" | ascii_downcase | contains($artist | ascii_downcase))))) | .[0])
    // ($songs | map(select((.title // "" | ascii_downcase | contains($title | ascii_downcase)))) | .[0])
    // $songs[0]
    // empty
' <<<"$SEARCH_JSON" 2>/dev/null || true)"
[[ -n "$SONG_JSON" ]] || exit 0

SONG_ID="$(jq -r '.id // empty' <<<"$SONG_JSON")"
[[ -n "$SONG_ID" ]] || exit 0

CODEC="$(jq -r '(.suffix // .contentType // "") | split("/") | last | ascii_downcase' <<<"$SONG_JSON")"
SRATE="$(jq -r '.samplingRate // 44100' <<<"$SONG_JSON")"
BITS="$(jq -r '.bitDepth // 16' <<<"$SONG_JSON")"

FMT_QS="format=raw"
if [[ "$QUALITY" == "small" ]]; then
  FMT_QS="maxBitRate=128&format=mp3"
  CODEC="mp3"
fi

ENC_ID="$(jq -rn --arg v "$SONG_ID" '$v | @uri')"
ENC_USER="$(jq -rn --arg v "$USER_NAME" '$v | @uri')"
STREAM_URL="${BASE_URL}/rest/stream.view?u=${ENC_USER}&t=${TOKEN}&s=${SALT}&v=1.16.1&c=musicctl&f=json&id=${ENC_ID}&${FMT_QS}"

jq -cn \
  --arg url "$STREAM_URL" \
  --arg codec "$CODEC" \
  --argjson bits "${BITS:-0}" \
  --argjson srate "${SRATE:-0}" \
  '{provider:"opensubsonic",url:$url,codec:$codec,bit_depth:$bits,sample_rate:$srate,headers:{}}'
