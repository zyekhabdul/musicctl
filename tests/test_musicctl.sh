#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN="$ROOT_DIR/bin/musicctl"
TMP_DIR="$(mktemp -d /tmp/musicctl-test.XXXXXX)"
export MUSICCTL_SOCKET="$TMP_DIR/musicctl.sock"
export MUSICCTL_DATA_DIR="$TMP_DIR/data"
export MUSICCTL_CACHE_DIR="$TMP_DIR/cache"
export MUSICCTL_CONFIG="$TMP_DIR/config"
export MUSICCTL_MPV_EXTRA_ARGS="--ao=null"
export MUSICCTL_DOWNLOAD_DIR="$TMP_DIR/downloads"
export MUSICCTL_NOTIFY=0
export MUSICCTL_CACHE=0
export MUSICCTL_ART=0
OFFLINE="${MUSICCTL_OFFLINE_TESTS:-0}"

PASS=0
FAIL=0

cleanup() {
  "$BIN" stop >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

assert_ok() {
  local name="$1"
  shift
  if "$@"; then
    printf '[PASS] %s\n' "$name"
    ((PASS++))
  else
    printf '[FAIL] %s\n' "$name" >&2
    ((FAIL++))
  fi
}

assert_contains() {
  local name="$1"
  local haystack="$2"
  local needle="$3"
  if grep -qF -- "$needle" <<<"$haystack"; then
    printf '[PASS] %s\n' "$name"
    ((PASS++))
  else
    printf '[FAIL] %s (expected %q in %q)\n' "$name" "$needle" "$haystack" >&2
    ((FAIL++))
  fi
}

# 1. Bash syntax check
assert_ok "bash syntax check" bash -n "$BIN"

# 2. Help output includes new commands
HELP_OUT="$("$BIN" --help)"
assert_contains "help output contains USAGE" "$HELP_OUT" "USAGE"
assert_contains "help output contains radio" "$HELP_OUT" "radio"
assert_contains "help output contains lyrics" "$HELP_OUT" "lyrics"
assert_contains "help output contains sleep" "$HELP_OUT" "sleep"
assert_contains "help output contains move" "$HELP_OUT" "move"
assert_contains "help output contains quality" "$HELP_OUT" "quality"
assert_contains "help output contains --quality" "$HELP_OUT" "--quality"
assert_contains "help output contains sync" "$HELP_OUT" "sync"
assert_contains "help output contains import" "$HELP_OUT" "import"

# 3. Idle status & JSON format
STATUS_IDLE="$("$BIN" status)"
assert_contains "idle status text" "$STATUS_IDLE" "[IDLE]"

STATUS_JSON="$("$BIN" status --json)"
assert_ok "idle status json valid" jq -e '.state == "idle" and .text == "[IDLE]" and .loop == "off" and .muted == false and .speed == 1' <<<"$STATUS_JSON" >/dev/null

# 4. Offline parsers: InnerTube Search, Radio, and LRCLIB Lyrics
FIXTURE_JSON='{
  "contents": {
    "sectionListRenderer": {
      "contents": [{
        "musicShelfRenderer": {
          "contents": [{
            "musicResponsiveListItemRenderer": {
              "playlistItemData": {"videoId": "dQw4w9WgXcQ"},
              "flexColumns": [
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Never Gonna Give You Up"}]}}},
                {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [{"text": "Rick Astley"}, {"text": " • "}, {"text": "3:33"}]}}}
              ]
            }
          }]
        }
      }]
    }
  }
}'
PARSED_FIXTURE="$(printf '%s' "$FIXTURE_JSON" | "$BIN" _parse_ytm)"
assert_contains "offline InnerTube parser videoId" "$PARSED_FIXTURE" "dQw4w9WgXcQ	Never Gonna Give You Up	Rick Astley • 3:33"

RADIO_FIXTURE='{
  "contents": {
    "singleColumnMusicWatchNextResultsRenderer": {
      "tabbedRenderer": {
        "watchNextTabbedResultsRenderer": {
          "tabs": [{
            "tabRenderer": {
              "content": {
                "musicQueueRenderer": {
                  "content": {
                    "playlistPanelRenderer": {
                      "contents": [{
                        "playlistPanelVideoRenderer": {
                          "videoId": "w_0RyTy-GlA",
                          "title": {"runs": [{"text": "Monolog"}]},
                          "longBylineText": {"runs": [{"text": "Pamungkas"}, {"text": " • "}, {"text": "Walk the Talk"}]},
                          "lengthText": {"runs": [{"text": "3:28"}]}
                        }
                      }]
                    }
                  }
                }
              }
            }
          }]
        }
      }
    }
  }
}'
PARSED_RADIO="$(printf '%s' "$RADIO_FIXTURE" | "$BIN" _parse_radio)"
assert_contains "offline Radio parser videoId" "$PARSED_RADIO" "w_0RyTy-GlA	Monolog	Pamungkas • Walk the Talk • 3:28"

LYRICS_PLAIN_FIXTURE='[{"trackName":"To the Bone","artistName":"Pamungkas","plainLyrics":"Have I ever told you\nI want you to the bone?"}]'
PARSED_PLAIN_LYRICS="$(printf '%s' "$LYRICS_PLAIN_FIXTURE" | "$BIN" _parse_lyrics)"
assert_contains "offline plain lyrics header" "$PARSED_PLAIN_LYRICS" "=== To the Bone - Pamungkas ==="
assert_contains "offline plain lyrics body" "$PARSED_PLAIN_LYRICS" "I want you to the bone?"

LYRICS_SYNCED_FIXTURE='[{"trackName":"Monolog","artistName":"Pamungkas","syncedLyrics":"[00:12.34] Gelap malam\n[00:15.00] Menemani"}]'
PARSED_SYNCED_LYRICS="$(printf '%s' "$LYRICS_SYNCED_FIXTURE" | "$BIN" _parse_lyrics)"
assert_contains "offline synced lyrics strips timestamps" "$PARSED_SYNCED_LYRICS" "Gelap malam"
assert_ok "offline synced lyrics has no bracket timestamps" bash -c '! grep -qF "[00:12" <<<"$1"' _ "$PARSED_SYNCED_LYRICS"

# 5. Favorites CRUD
assert_ok "fav add" "$BIN" fav add "IR7tYHdYN2Q" "To the Bone" "Pamungkas • 5:45" >/dev/null
FAV_OUT="$("$BIN" fav list)"
assert_contains "fav list shows track" "$FAV_OUT" "To the Bone"
assert_ok "fav rm" "$BIN" fav rm "IR7tYHdYN2Q" >/dev/null
FAV_EMPTY="$("$BIN" fav list)"
assert_contains "fav list empty after rm" "$FAV_EMPTY" "No favorites saved"

# 6. Named Playlists CRUD & validation
assert_ok "pl create chill" "$BIN" pl create chill >/dev/null
assert_ok "pl invalid name rejected" bash -c '! "$1" pl create "../evil" >/dev/null 2>&1' _ "$BIN"
assert_ok "pl add track" "$BIN" pl add chill "IR7tYHdYN2Q" >/dev/null
PL_LS="$("$BIN" pl ls)"
assert_contains "pl ls shows chill (1 tracks)" "$PL_LS" "chill"
PL_SHOW="$("$BIN" pl show chill)"
assert_contains "pl show displays track ID" "$PL_SHOW" "IR7tYHdYN2Q"
assert_ok "pl rm track" "$BIN" pl rm chill "IR7tYHdYN2Q" >/dev/null
assert_ok "pl delete" "$BIN" pl delete chill >/dev/null

# 7. Local library listing
LOCAL_EMPTY="$("$BIN" local)"
assert_contains "local empty dir message" "$LOCAL_EMPTY" "No local audio files"
mkdir -p "$MUSICCTL_DOWNLOAD_DIR"
: >"$MUSICCTL_DOWNLOAD_DIR/Sample - Track [abc12345678].opus"
LOCAL_OUT="$("$BIN" local)"
assert_contains "local lists opus track" "$LOCAL_OUT" "Sample - Track [abc12345678].opus"
mkdir -p "$MUSICCTL_DOWNLOAD_DIR/Artist/Album/Disc1" "$MUSICCTL_DOWNLOAD_DIR/.secret"
: >"$MUSICCTL_DOWNLOAD_DIR/Artist/Album/Disc1/depth4.webm"
: >"$MUSICCTL_DOWNLOAD_DIR/.secret/hidden.flac"
LOCAL_VIEW="$("$BIN" _view local)"
assert_contains "_view local matches depth-4 webm like library.py" "$LOCAL_VIEW" "depth4.webm"
assert_ok "_view local skips hidden dirs like library.py" bash -c '! grep -qF "hidden.flac" <<<"$1"' _ "$LOCAL_VIEW"

# 8. Isolated live mpv daemon lifecycle & playback modes (--ao=null)
VOL_OUT="$("$BIN" vol 65)"
assert_contains "daemon start + vol set" "$VOL_OUT" "Volume: 65%"
assert_ok "daemon socket exists" test -S "$MUSICCTL_SOCKET"

MUTE_ON="$("$BIN" mute)"
assert_contains "mute toggle on" "$MUTE_ON" "Mute: on"
MUTE_OFF="$("$BIN" mute)"
assert_contains "mute toggle off" "$MUTE_OFF" "Mute: off"

LOOP_ONE="$("$BIN" loop one)"
assert_contains "loop one" "$LOOP_ONE" "Loop: one"
LOOP_ALL="$("$BIN" loop cycle)"
assert_contains "loop cycle one->all" "$LOOP_ALL" "Loop: all"
LOOP_OFF="$("$BIN" loop cycle)"
assert_contains "loop cycle all->off" "$LOOP_OFF" "Loop: off"

SPEED_OUT="$("$BIN" speed 1.5)"
assert_contains "speed 1.5x" "$SPEED_OUT" "Speed: 1.5x"
SPEED_RST="$("$BIN" speed reset)"
assert_contains "speed reset" "$SPEED_RST" "Speed: 1.0x"

assert_ok "shuffle on daemon" "$BIN" shuffle >/dev/null
assert_ok "clear queue on daemon" "$BIN" clear >/dev/null

DAEMON_JSON="$("$BIN" status --json)"
assert_ok "daemon status json schema" jq -e '.state == "idle" and .muted == false and .loop == "off" and .speed == 1' <<<"$DAEMON_JSON" >/dev/null

# 9. Sleep timer start -> status -> cancel
SLEEP_SET="$("$BIN" sleep 5)"
assert_contains "sleep timer set 5m" "$SLEEP_SET" "5m"
SLEEP_STAT="$("$BIN" sleep status)"
assert_contains "sleep timer active status" "$SLEEP_STAT" "Stopping playback in"
SLEEP_OFF="$("$BIN" sleep off)"
assert_contains "sleep timer cancelled" "$SLEEP_OFF" "Sleep timer cancelled"
SLEEP_IDLE="$("$BIN" sleep status)"
assert_contains "sleep timer inactive after cancel" "$SLEEP_IDLE" "Sleep timer inactive"

STOP_OUT="$("$BIN" stop)"
assert_contains "daemon stop" "$STOP_OUT" "Daemon stopped"
assert_ok "daemon socket cleaned up" test ! -S "$MUSICCTL_SOCKET"

# 9b. Quoted MUSICCTL_MPV_EXTRA_ARGS preserves spaces via shlex split
EXTRA_VOL="$(MUSICCTL_MPV_EXTRA_ARGS='--ao=null --title="Musicctl Space Test"' "$BIN" vol 70)"
assert_contains "daemon starts with quoted extra args" "$EXTRA_VOL" "Volume: 70%"
EXTRA_TITLE="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import common; print((common.MpvClient(sys.argv[2]).batch_get(["title"]) or {}).get("title", ""))' "$ROOT_DIR/lib" "$MUSICCTL_SOCKET")"
assert_contains "quoted extra arg title preserved" "$EXTRA_TITLE" "Musicctl Space Test"
"$BIN" stop >/dev/null
assert_ok "daemon socket cleaned up after extra args test" test ! -S "$MUSICCTL_SOCKET"

# 10. Version, doctor, config precedence, synced lyrics JSON, and auto-cache resolution
VER_OUT="$("$BIN" --version)"
assert_contains "version prints 2.3.0" "$VER_OUT" "musicctl 2.3.0"

DOC_OUT="$("$BIN" doctor)"
assert_contains "doctor checks mpv" "$DOC_OUT" "mpv"
assert_contains "doctor checks chafa" "$DOC_OUT" "chafa"

# EQ & Loudness normalization
EQ_BASS="$("$BIN" eq bass)"
assert_contains "eq set bass" "$EQ_BASS" "EQ: bass"
EQ_CYCLE="$("$BIN" eq cycle)"
assert_contains "eq cycle bass->warm" "$EQ_CYCLE" "EQ: warm"
EQ_FLAT="$("$BIN" eq flat)"
assert_contains "eq set flat" "$EQ_FLAT" "EQ: flat"
assert_ok "eq invalid preset rejected" bash -c '! "$1" eq invalid_preset >/dev/null 2>&1' _ "$BIN"

NORM_ON="$("$BIN" norm on)"
assert_contains "norm on" "$NORM_ON" "Loudness norm: on"
NORM_OFF="$("$BIN" norm toggle)"
assert_contains "norm toggle off" "$NORM_OFF" "Loudness norm: off"

DSP_JSON="$("$BIN" status --json)"
assert_ok "status json includes eq, norm, codec, samplerate" jq -e 'has("eq") and has("norm") and has("codec") and has("samplerate") and .eq == "flat" and .norm == false' <<<"$DSP_JSON" >/dev/null

cat >"$MUSICCTL_CONFIG" <<'CFG'
MUSICCTL_CACHE=1
CFG
CFG_ON="$(env -u MUSICCTL_CACHE MUSICCTL_CONFIG="$MUSICCTL_CONFIG" "$BIN" cache status)"
assert_contains "config enables auto-cache when env unset" "$CFG_ON" "Auto-cache: on"
CFG_OVERRIDE="$(MUSICCTL_CACHE=0 MUSICCTL_CONFIG="$MUSICCTL_CONFIG" "$BIN" cache status)"
assert_contains "env overrides config file" "$CFG_OVERRIDE" "Auto-cache: off"
rm -f "$MUSICCTL_CONFIG"

SYNCED_PARSED="$(printf '%s' "$LYRICS_SYNCED_FIXTURE" | "$BIN" _parse_synced_lyrics)"
assert_ok "offline synced lyrics json valid" jq -e '.title == "Monolog" and (.lines | length == 2) and .lines[0].t == 12.34' <<<"$SYNCED_PARSED" >/dev/null

MISS_TARGET="$("$BIN" _resolve_play_target "IR7tYHdYN2Q")"
assert_contains "cache miss resolves remote URL" "$MISS_TARGET" "https://www.youtube.com/watch?v=IR7tYHdYN2Q"
mkdir -p "$MUSICCTL_CACHE_DIR/audio"
printf 'dummy' >"$MUSICCTL_CACHE_DIR/audio/IR7tYHdYN2Q.opus"
HIT_TARGET="$("$BIN" _resolve_play_target "IR7tYHdYN2Q")"
assert_contains "cache hit resolves local opus" "$HIT_TARGET" "$MUSICCTL_CACHE_DIR/audio/IR7tYHdYN2Q.opus"
CACHE_CLR="$("$BIN" cache clear)"
assert_contains "cache clear succeeds" "$CACHE_CLR" "Cache cleared"
assert_ok "cached opus deleted after clear" test ! -f "$MUSICCTL_CACHE_DIR/audio/IR7tYHdYN2Q.opus"

# 11. Session persistence (save -> stop -> resume) using generated silent WAV
WAV_FILE="$TMP_DIR/silence.wav"
python3 - "$WAV_FILE" <<'PY'
import sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\x00\x00" * 80000)
PY
assert_ok "play local wav" "$BIN" play "$WAV_FILE" >/dev/null
assert_ok "set session volume 72" "$BIN" vol 72 >/dev/null
assert_ok "set session loop all" "$BIN" loop all >/dev/null
assert_ok "save session" "$BIN" save >/dev/null
assert_ok "session.json exists" test -s "$MUSICCTL_DATA_DIR/session.json"
assert_ok "stop daemon" "$BIN" stop >/dev/null
RESUME_OUT="$("$BIN" resume)"
assert_contains "resume restores session" "$RESUME_OUT" "Resumed session"
RESUMED_JSON="$("$BIN" status --json)"
assert_ok "resumed volume & loop preserved" jq -e '.volume == 72 and .loop == "all"' <<<"$RESUMED_JSON" >/dev/null
PREVIEW_OUT="$("$BIN" _preview $'IR7tYHdYN2Q\tTo the Bone\tPamungkas • 5:45\t[YTM]')"
assert_ok "preview hides 1.000000x speed" bash -c '! grep -qF "1.000000x" <<<"$1"' _ "$PREVIEW_OUT"
"$BIN" stop >/dev/null

# 12. Desktop entry validation & install.sh dry-install/uninstall
if command -v desktop-file-validate >/dev/null 2>&1; then
  assert_ok "desktop-file-validate musicctl.desktop" desktop-file-validate "$ROOT_DIR/musicctl.desktop"
fi
PREFIX_TMP="$TMP_DIR/prefix"
mkdir -p "$PREFIX_TMP/share/musicctl/__pycache__"
printf 'legacy\n' >"$PREFIX_TMP/share/musicctl/common.py"
printf 'user-fav\n' >"$PREFIX_TMP/share/musicctl/favorites.tsv"
assert_ok "install.sh dry install" "$ROOT_DIR/install.sh" --prefix "$PREFIX_TMP" >/dev/null
assert_ok "installed bin exists" test -x "$PREFIX_TMP/bin/musicctl"
assert_ok "installed common.py exists" test -f "$PREFIX_TMP/lib/musicctl/common.py"
assert_ok "installed catalog.py exists" test -f "$PREFIX_TMP/lib/musicctl/catalog.py"
assert_ok "installed providers.py exists" test -f "$PREFIX_TMP/lib/musicctl/providers.py"
assert_ok "installed downloader.py exists" test -f "$PREFIX_TMP/lib/musicctl/downloader.py"
assert_ok "installed library.py exists" test -f "$PREFIX_TMP/lib/musicctl/library.py"
assert_ok "installed tui_widgets.py exists" test -f "$PREFIX_TMP/lib/musicctl/tui_widgets.py"
assert_ok "installed tui.py exists" test -f "$PREFIX_TMP/lib/musicctl/tui.py"
assert_ok "installed mpv_hooks.lua exists" test -f "$PREFIX_TMP/lib/musicctl/mpv_hooks.lua"
assert_ok "legacy share common.py migrated away" test ! -e "$PREFIX_TMP/share/musicctl/common.py"
assert_ok "legacy share __pycache__ migrated away" test ! -e "$PREFIX_TMP/share/musicctl/__pycache__"
assert_ok "user favorites.tsv preserved during install" test -f "$PREFIX_TMP/share/musicctl/favorites.tsv"
assert_ok "installed desktop exists" test -f "$PREFIX_TMP/share/applications/musicctl.desktop"
PREFIX_STATUS="$("$PREFIX_TMP/bin/musicctl" status --json)"
assert_ok "installed prefix status json valid" jq -e '.state == "idle"' <<<"$PREFIX_STATUS" >/dev/null
assert_ok "install.sh uninstall" "$ROOT_DIR/install.sh" --prefix "$PREFIX_TMP" --uninstall >/dev/null
assert_ok "installed bin removed" test ! -e "$PREFIX_TMP/bin/musicctl"
assert_ok "installed common.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/common.py"
assert_ok "installed catalog.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/catalog.py"
assert_ok "installed providers.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/providers.py"
assert_ok "installed downloader.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/downloader.py"
assert_ok "installed library.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/library.py"
assert_ok "installed tui_widgets.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/tui_widgets.py"
assert_ok "installed tui.py removed" test ! -e "$PREFIX_TMP/lib/musicctl/tui.py"
assert_ok "installed mpv_hooks.lua removed" test ! -e "$PREFIX_TMP/lib/musicctl/mpv_hooks.lua"
assert_ok "user favorites.tsv preserved after uninstall" test -f "$PREFIX_TMP/share/musicctl/favorites.tsv"

# 12b. Pure-Bash format_mmss and ascii_bar boundary checks
assert_contains "format_mmss 0 -> 00:00" "$("$BIN" _format_mmss 0)" "00:00"
assert_contains "format_mmss 009.8 -> 00:09" "$("$BIN" _format_mmss 009.8)" "00:09"
assert_contains "format_mmss 65.9 -> 01:05" "$("$BIN" _format_mmss 65.9)" "01:05"
assert_contains "format_mmss 3661.2 -> 1:01:01" "$("$BIN" _format_mmss 3661.2)" "1:01:01"
assert_contains "format_mmss -5 -> 00:00" "$("$BIN" _format_mmss -5)" "00:00"
assert_contains "ascii_bar dur 0" "$("$BIN" _ascii_bar 0 0 10)" "[----------]"
assert_contains "ascii_bar pos 0" "$("$BIN" _ascii_bar 0 100 10)" "[>---------]"
assert_contains "ascii_bar mid" "$("$BIN" _ascii_bar 50 100 10)" "[====>-----]"
assert_contains "ascii_bar full" "$("$BIN" _ascii_bar 100 100 10)" "[==========]"

# 13. WP1B-WP1F: Queue move, pl move/sync/prune, fallback lyrics, Spotify HTML parser, 3-tier quality & pluggable providers
# 13a. Playlist TSV move (WP1B)
assert_ok "pl create mix" "$BIN" pl create mix >/dev/null
printf 'trkA1111111\tFirst Song\tArtist A • 3:00\ntrkB2222222\tSecond Song\tArtist B • 3:10\ntrkC3333333\tThird Song\tArtist C • 3:20\n' >"$MUSICCTL_DATA_DIR/playlists/mix.tsv"
assert_ok "pl move 3 -> 1" "$BIN" pl move mix 3 1 >/dev/null
PL_MOVED="$(head -n 1 "$MUSICCTL_DATA_DIR/playlists/mix.tsv")"
assert_contains "pl move puts Third Song first" "$PL_MOVED" "trkC3333333"

# 13b. Live mpv queue move (WP1B) with 3 generated WAV tracks
WAV1="$TMP_DIR/q1.wav"
WAV2="$TMP_DIR/q2.wav"
WAV3="$TMP_DIR/q3.wav"
cp -f "$WAV_FILE" "$WAV1" && cp -f "$WAV_FILE" "$WAV2" && cp -f "$WAV_FILE" "$WAV3"
assert_ok "play q1" "$BIN" play "$WAV1" >/dev/null
assert_ok "add q2" "$BIN" add "$WAV2" >/dev/null
assert_ok "add q3" "$BIN" add "$WAV3" >/dev/null
assert_ok "move queue 1 -> 3" "$BIN" move 1 3 >/dev/null
Q_AFTER_DOWN="$("$BIN" queue | sed -E $'s/\x1b\\[[0-9;]*m//g')"
assert_ok "queue move down shows #1 q2" bash -c 'grep -qE "1\..*q2\.wav" <<<"$1"' _ "$Q_AFTER_DOWN"
assert_ok "queue move down shows #3 q1" bash -c 'grep -qE "3\..*q1\.wav" <<<"$1"' _ "$Q_AFTER_DOWN"
assert_ok "move queue 3 -> 1" "$BIN" move 3 1 >/dev/null
Q_AFTER_UP="$("$BIN" queue | sed -E $'s/\x1b\\[[0-9;]*m//g')"
assert_ok "queue move up restores #1 q1" bash -c 'grep -qE "1\..*q1\.wav" <<<"$1"' _ "$Q_AFTER_UP"
"$BIN" stop >/dev/null

# 13c. Offline pl sync cache skip + prune (WP1C)
mkdir -p "$MUSICCTL_CACHE_DIR/audio"
printf 'cached1' >"$MUSICCTL_CACHE_DIR/audio/trkA1111111.opus"
printf 'cached2' >"$MUSICCTL_CACHE_DIR/audio/trkB2222222.opus"
printf 'cached3' >"$MUSICCTL_CACHE_DIR/audio/trkC3333333.opus"
printf 'orphan' >"$MUSICCTL_CACHE_DIR/audio/orphan99999.opus"
SYNC_OUT="$("$BIN" pl sync mix --prune)"
assert_contains "pl sync skips 3 cached tracks" "$SYNC_OUT" "0 fetched, 3 cached, 0 failed"
assert_ok "pl sync --prune deletes orphan cache file" test ! -f "$MUSICCTL_CACHE_DIR/audio/orphan99999.opus"
assert_ok "pl sync --prune keeps playlist track cache" test -f "$MUSICCTL_CACHE_DIR/audio/trkA1111111.opus"
"$BIN" pl delete mix >/dev/null
"$BIN" cache clear >/dev/null

# 13d. Fallback lyrics parser (WP1D)
FB_JSON='{"lyrics":"Paroles de la chanson Test par Artist\r\nLine one\nLine two"}'
FB_PARSED="$(printf '%s' "$FB_JSON" | "$BIN" _parse_fallback_lyrics "Test" "Artist")"
assert_contains "fallback lyrics includes [Fallback] header" "$FB_PARSED" "=== Test - Artist [Fallback] ==="
assert_contains "fallback lyrics strips boilerplate and keeps body" "$FB_PARSED" "Line one"
assert_ok "fallback lyrics stripped French prefix" bash -c '! grep -qF "Paroles de la chanson" <<<"$1"' _ "$FB_PARSED"

# 13e. Public Spotify playlist HTML & OpenGraph fixtures (WP1E)
SP_NEXT_HTML='<html><head><script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"state":{"data":{"entity":{"trackList":[{"title":"One Only","subtitle":"Pamungkas"},{"title":"Monolog","artists":[{"name":"Pamungkas"}]}]}}}}}}</script></head></html>'
SP_PARSED="$(printf '%s' "$SP_NEXT_HTML" | "$BIN" _parse_spotify_html)"
assert_contains "spotify __NEXT_DATA__ extracts One Only" "$SP_PARSED" "One Only	Pamungkas"
assert_contains "spotify __NEXT_DATA__ extracts Monolog" "$SP_PARSED" "Monolog	Pamungkas"

SP_OG_HTML='<html><head><meta property="og:title" content="sailing" /><meta property="og:description" content="grentperez · Song · 2024" /></head></html>'
SP_OG_PARSED="$(printf '%s' "$SP_OG_HTML" | "$BIN" _parse_spotify_html)"
assert_contains "spotify OpenGraph extracts single track" "$SP_OG_PARSED" "sailing	grentperez"

# 13f. 3-tier quality state, ffprobe validator, ordered provider chain & cover/tag embed (WP1F)
Q_DEF="$("$BIN" quality status)"
assert_contains "default quality is cd" "$Q_DEF" "Quality: cd"
assert_ok "set quality hires" "$BIN" quality hires >/dev/null
assert_contains "quality persists hires" "$("$BIN" quality status)" "Quality: hires"
assert_contains "quality cycle hires->ask" "$("$BIN" quality cycle)" "Quality: ask"
assert_contains "quality cycle ask->small" "$("$BIN" quality cycle)" "Quality: small"
assert_ok "invalid quality tier rejected" bash -c '! "$1" quality ultra >/dev/null 2>&1' _ "$BIN"
assert_ok "set quality back to cd" "$BIN" quality cd >/dev/null

FLAC_SRC="$TMP_DIR/sample10.flac"
ART_SRC="$MUSICCTL_CACHE_DIR/art/dQw4w9WgXcQ.jpg"
mkdir -p "$MUSICCTL_CACHE_DIR/art"
ffmpeg -y -v error -f lavfi -i anullsrc=r=44100:cl=stereo -t 10 -c:a flac "$FLAC_SRC"
ffmpeg -y -v error -f lavfi -i color=c=blue:s=64x64:d=1 -frames:v 1 "$ART_SRC"

VAL_OK="$("$BIN" _validate_audio "$FLAC_SRC" 10 1)"
assert_ok "ffprobe validates 10s lossless FLAC" jq -e '.codec == "flac" and .sample_rate == 44100' <<<"$VAL_OK" >/dev/null
assert_ok "ffprobe rejects duration mismatch" bash -c '! "$1" _validate_audio "$2" 180 0 >/dev/null 2>&1' _ "$BIN" "$FLAC_SRC"

PROV_DIR="$TMP_DIR/providers"
mkdir -p "$PROV_DIR"
cat >"$PROV_DIR/10-empty.sh" <<'PROV1'
#!/usr/bin/env bash
cat >/dev/null
exit 0
PROV1
cat >"$PROV_DIR/20-mock-flac.sh" <<PROV2
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"flac","bit_depth":16,"sample_rate":44100}\n' "$FLAC_SRC"
PROV2
chmod +x "$PROV_DIR/10-empty.sh" "$PROV_DIR/20-mock-flac.sh"
export MUSICCTL_PROVIDERS_DIR="$PROV_DIR"

PROV_RES="$("$BIN" _resolve_provider "download" "cd" "dQw4w9WgXcQ" "Test Title" "Test Artist" "" 10)"
assert_ok "provider chain skips empty 10-empty and picks 20-mock-flac" jq -e '.provider == "20-mock-flac.sh" and .codec == "flac"' <<<"$PROV_RES" >/dev/null

rm -rf "${MUSICCTL_DOWNLOAD_DIR:?}"/*
DL_CD_OUT="$("$BIN" dl --quality cd "dQw4w9WgXcQ")"
assert_contains "dl --quality cd uses mock flac provider" "$DL_CD_OUT" "20-mock-flac.sh"
CD_FLAC_OUT="$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
assert_ok "dl --quality cd writes .flac file" test -s "$CD_FLAC_OUT"
FLAC_PROBE="$(ffprobe -v error -show_entries stream=codec_type,codec_name:format_tags=title -of json "$CD_FLAC_OUT")"
assert_ok "dl --quality cd embeds title tag and cover art" jq -e '((.format.tags.title // .format.tags.TITLE) == "dQw4w9WgXcQ") and ([.streams[].codec_type] | index("video") != null)' <<<"$FLAC_PROBE" >/dev/null

rm -f "$CD_FLAC_OUT"
DL_SM_OUT="$("$BIN" dl --quality small --format m4a "dQw4w9WgXcQ")"
assert_contains "dl --quality small transcodes FLAC to AAC" "$DL_SM_OUT" "aac"
SM_M4A_OUT="$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.m4a"
assert_ok "dl --quality small --format m4a writes .m4a file" test -s "$SM_M4A_OUT"
M4A_PROBE="$(ffprobe -v error -show_entries stream=codec_name:format_tags=title -of json "$SM_M4A_OUT")"
assert_ok "dl --quality small produces valid AAC M4A with title tag" jq -e '(.streams[0].codec_name == "aac") and (.format.tags.title == "dQw4w9WgXcQ")' <<<"$M4A_PROBE" >/dev/null
BAD_COVER="$TMP_DIR/bad-cover.jpg"
printf 'not-an-image' >"$BAD_COVER"
FB_FLAC="$TMP_DIR/fb-spaced.flac"
assert_ok "embed_metadata_cover fallback preserves spaced tags" "$BIN" _embed_metadata_cover "$FLAC_SRC" "$FB_FLAC" "To the Bone" "Pamungkas & Friends" "Walk the Talk" "$BAD_COVER"
FB_PROBE="$(ffprobe -v error -show_entries format_tags=title,artist,album -of json "$FB_FLAC")"
assert_ok "embed_metadata_cover fallback writes title/artist/album tags" jq -e '((.format.tags.title // .format.tags.TITLE) == "To the Bone") and ((.format.tags.artist // .format.tags.ARTIST) == "Pamungkas & Friends") and ((.format.tags.album // .format.tags.ALBUM) == "Walk the Talk")' <<<"$FB_PROBE" >/dev/null

# 13g. P3: Escaped mpv headers, preflight stream probe, and lossless WAV small transcode
HDR_ESC="$(printf '%s' '{"headers":{"Cookie":"a=1, b=2","X-Path":"C:\\dir\\f"}}' | "$BIN" _format_mpv_headers)"
assert_contains "format_mpv_headers escapes comma and backslash" "$HDR_ESC" 'Cookie: a=1\, b=2,X-Path: C:\\dir\\f'

assert_ok "probe_provider_stream accepts non-empty local file" "$BIN" _probe_provider_stream "file://$FLAC_SRC" '{}'
assert_ok "probe_provider_stream rejects missing local file" bash -c '! "$1" _probe_provider_stream "/nonexistent/missing.flac" "{}"' _ "$BIN"

HTTP_PORT_FILE="$TMP_DIR/http.port"
python3 - "$HTTP_PORT_FILE" <<'PY' &
import http.server
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        if self.path == "/ok":
            self.send_response(200)
        elif self.path == "/no-head":
            self.send_response(405)
        else:
            self.send_response(403)
        self.end_headers()

    def do_GET(self):
        if self.path in ("/ok", "/no-head"):
            self.send_response(206 if self.headers.get("Range") else 200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(403)
            self.end_headers()

    def log_message(self, *args):
        pass

srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(str(srv.server_address[1]))
srv.serve_forever()
PY
HTTP_PID=$!
for _ in $(seq 1 30); do
  [[ -s "$HTTP_PORT_FILE" ]] && break
  sleep 0.05
done
HTTP_PORT="$(cat "$HTTP_PORT_FILE")"
assert_ok "probe_provider_stream HTTP 200 HEAD passes" "$BIN" _probe_provider_stream "http://127.0.0.1:${HTTP_PORT}/ok" '{}'
assert_ok "probe_provider_stream HTTP 405 HEAD retries Range GET 206" "$BIN" _probe_provider_stream "http://127.0.0.1:${HTTP_PORT}/no-head" '{}'
assert_ok "probe_provider_stream HTTP 403 rejected" bash -c '! "$1" _probe_provider_stream "http://127.0.0.1:${HTTP_PORT}/denied" "{}"' _ "$BIN"
kill "$HTTP_PID" 2>/dev/null || true
wait "$HTTP_PID" 2>/dev/null || true

WAV10="$TMP_DIR/sample10.wav"
ffmpeg -y -v error -f lavfi -i anullsrc=r=44100:cl=stereo -t 10 -c:a pcm_s16le "$WAV10"
cat >"$PROV_DIR/20-mock-flac.sh" <<PROV_WAV
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"wav","bit_depth":16,"sample_rate":44100}\n' "$WAV10"
PROV_WAV
chmod +x "$PROV_DIR/20-mock-flac.sh"
rm -f "$SM_M4A_OUT"
DL_WAV_SM="$("$BIN" dl --quality small --format m4a "dQw4w9WgXcQ")"
assert_contains "dl --quality small transcodes WAV pcm_s16le to AAC" "$DL_WAV_SM" "aac (from pcm_s16le)"
assert_ok "dl --quality small from WAV writes .m4a file" test -s "$SM_M4A_OUT"
unset MUSICCTL_PROVIDERS_DIR

# 13h. Multi-candidate fallback, strict cd/hires validation, stream probe fallback, sleep PGID kill, and doctor quality format
AAC10="$TMP_DIR/lossy10.m4a"
HIRES10="$TMP_DIR/hires10.flac"
ffmpeg -y -v error -f lavfi -i anullsrc=r=44100:cl=stereo -t 2 -c:a aac -b:a 96k "$AAC10"
ffmpeg -y -v error -f lavfi -i anullsrc=r=96000:cl=stereo -t 2 -c:a flac -sample_fmt s32 "$HIRES10"

assert_ok "ffprobe rejects 16-bit 44.1kHz FLAC for hires" bash -c '! "$1" _validate_audio "$2" 10 1 1 >/dev/null 2>&1' _ "$BIN" "$FLAC_SRC"
HIRES_VAL="$("$BIN" _validate_audio "$HIRES10" 2 1 1)"
assert_ok "ffprobe accepts 96kHz FLAC for hires" jq -e '.codec == "flac" and .sample_rate == 96000' <<<"$HIRES_VAL" >/dev/null

PROV_MULTI="$TMP_DIR/providers-multi"
mkdir -p "$PROV_MULTI"
cat >"$PROV_MULTI/10-lossy.sh" <<PROV_M1
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"aac","bit_depth":0,"sample_rate":44100}\n' "$AAC10"
PROV_M1
cat >"$PROV_MULTI/20-cd.sh" <<PROV_M2
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"flac","bit_depth":16,"sample_rate":44100}\n' "$FLAC_SRC"
PROV_M2
cat >"$PROV_MULTI/30-hires.sh" <<PROV_M3
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"flac","bit_depth":24,"sample_rate":96000}\n' "$HIRES10"
PROV_M3
chmod +x "$PROV_MULTI"/*.sh

export MUSICCTL_PROVIDERS_DIR="$PROV_MULTI"
rm -f "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
DL_MULTI_CD="$("$BIN" dl --quality cd --force "dQw4w9WgXcQ")"
assert_contains "dl --quality cd skips lossy provider and picks 20-cd.sh" "$DL_MULTI_CD" "20-cd.sh"

rm -f "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
DL_MULTI_HR="$("$BIN" dl --quality hires --force "dQw4w9WgXcQ")"
assert_contains "dl --quality hires skips cd and picks 30-hires.sh" "$DL_MULTI_HR" "30-hires.sh"

rm -f "$PROV_MULTI/30-hires.sh" "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
DL_CASC_HR="$("$BIN" dl --quality hires --force "dQw4w9WgXcQ")"
assert_contains "dl --quality hires cascades to cd provider when hires missing" "$DL_CASC_HR" "20-cd.sh"

PROV_STREAM="$TMP_DIR/providers-stream"
mkdir -p "$PROV_STREAM"
cat >"$PROV_STREAM/10-missing.sh" <<'PROV_S1'
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"/nonexistent/missing.flac","codec":"flac"}\n'
PROV_S1
cat >"$PROV_STREAM/20-wav.sh" <<PROV_S2
#!/usr/bin/env bash
cat >/dev/null
printf '{"url":"%s","codec":"wav","bit_depth":16,"sample_rate":44100}\n' "$WAV10"
PROV_S2
chmod +x "$PROV_STREAM"/*.sh
export MUSICCTL_PROVIDERS_DIR="$PROV_STREAM"
assert_ok "play falls back to 2nd stream provider on probe failure" "$BIN" play "dQw4w9WgXcQ" >/dev/null
STREAM_JSON="$("$BIN" status --json)"
assert_ok "stream fallback loads wav into mpv" jq -e '.state == "playing" and .codec == "pcm_s16le"' <<<"$STREAM_JSON" >/dev/null
"$BIN" stop >/dev/null
unset MUSICCTL_PROVIDERS_DIR

assert_ok "sleep 9 starts timer" "$BIN" sleep 9 >/dev/null
SLEEP_PGID="$(cut -d: -f1 "$MUSICCTL_DATA_DIR/sleep.state")"
SLEEP_CHILD="$(pgrep -P "$SLEEP_PGID" | head -n 1 || true)"
assert_ok "sleep child process running" test -n "$SLEEP_CHILD"
assert_ok "sleep off kills timer" "$BIN" sleep off >/dev/null
sleep 0.1
assert_ok "sleep child process terminated (no orphan)" bash -c '! kill -0 "$1" 2>/dev/null' _ "$SLEEP_CHILD"

DOC_SMALL="$(MUSICCTL_QUALITY=small "$BIN" doctor)"
assert_contains "doctor reflects small quality format" "$DOC_SMALL" "249/250/139"

# 13i. WP2A: Multi-catalog help, config precedence, parse-stdin fixtures, invalid flag rejection, and ytm:song ref play
HELP_WP2A="$("$BIN" --help)"
assert_contains "help lists browse command" "$HELP_WP2A" "browse"
assert_contains "help lists --catalog flag" "$HELP_WP2A" "--catalog"
assert_contains "help lists --type flag" "$HELP_WP2A" "--type"

cat >"$MUSICCTL_CONFIG" <<'CFG_CAT'
MUSICCTL_CATALOG=badcfgcat
CFG_CAT
CAT_CFG_ERR="$(env -u MUSICCTL_CATALOG MUSICCTL_CONFIG="$MUSICCTL_CONFIG" "$BIN" search "hello" 2>&1 || true)"
assert_contains "config sets MUSICCTL_CATALOG when env unset" "$CAT_CFG_ERR" "Unknown catalog 'badcfgcat'"
CAT_ENV_ERR="$(MUSICCTL_CATALOG=badenvcat MUSICCTL_CONFIG="$MUSICCTL_CONFIG" "$BIN" search "hello" 2>&1 || true)"
assert_contains "env overrides MUSICCTL_CATALOG from config" "$CAT_ENV_ERR" "Unknown catalog 'badenvcat'"
rm -f "$MUSICCTL_CONFIG"

assert_ok "search rejects invalid --catalog" bash -c '! "$1" search --catalog spotify "hello" >/dev/null 2>&1' _ "$BIN"
assert_ok "search rejects invalid --type" bash -c '! "$1" search --type podcasts "hello" >/dev/null 2>&1' _ "$BIN"

DZ_FIXTURE='{"data":[{"id":3135556,"title":"HBFS","isrc":"GBDUW0000059","duration":224,"track_position":4,"artist":{"name":"Daft Punk"},"album":{"title":"Discovery","release_date":"2001-03-12","cover_xl":"https://cdn/1000x1000.jpg"}}]}'
DZ_PARSE="$(printf '%s' "$DZ_FIXTURE" | python3 "$ROOT_DIR/lib/catalog.py" parse-stdin deezer search songs)"
assert_ok "catalog.py parse-stdin deezer extracts ISRC and album" jq -e '.[0].source == "deezer" and .[0].isrc == "GBDUW0000059" and .[0].album == "Discovery"' <<<"$DZ_PARSE" >/dev/null

IT_FIXTURE='{"results":[{"wrapperType":"track","trackId":1440857781,"trackName":"Get Lucky","artistName":"Daft Punk","collectionName":"RAM","trackNumber":8,"trackTimeMillis":369626,"releaseDate":"2013-05-17T07:00:00Z","artworkUrl100":"https://is1/100x100bb.jpg"}]}'
IT_PARSE="$(printf '%s' "$IT_FIXTURE" | python3 "$ROOT_DIR/lib/catalog.py" parse-stdin itunes search songs)"
assert_ok "catalog.py parse-stdin itunes upgrades 1000x1000bb cover" jq -e '.[0].source == "itunes" and (.[0].cover_url | endswith("1000x1000bb.jpg"))' <<<"$IT_PARSE" >/dev/null

export MUSICCTL_PROVIDERS_DIR="$PROV_STREAM"
assert_ok "play ytm:song ref resolves through mock WAV provider" "$BIN" play "ytm:song:dQw4w9WgXcQ" >/dev/null
REF_JSON="$("$BIN" status --json)"
assert_ok "play ytm:song ref loads WAV into isolated mpv" jq -e '.state == "playing" and .codec == "pcm_s16le"' <<<"$REF_JSON" >/dev/null
"$BIN" stop >/dev/null
unset MUSICCTL_PROVIDERS_DIR

# 13j. WP2B: Album/ISRC/year/track_num & clean title propagation, provider JSON stdin, and FLAC/M4A tags
assert_contains "_parse_meta_album extracts album" "$("$BIN" _parse_meta_album "Pamungkas • Walk the Talk • 3:28")" "Walk the Talk"
assert_ok "_parse_meta_album ignores duration-only meta" test -z "$("$BIN" _parse_meta_album "Pamungkas • 3:28")"
assert_contains "_clean_title strips Official Video" "$("$BIN" _clean_title "To the Bone (Official Video) [HD]")" "To the Bone"
assert_contains "_clean_title preserves Acoustic" "$("$BIN" _clean_title "Sailing (Acoustic) [Lyrics]")" "Sailing (Acoustic)"

TAGGED_FLAC="$TMP_DIR/tagged_wp2b.flac"
ffmpeg -y -v error -f lavfi -i anullsrc=r=44100:cl=stereo -t 10 -c:a flac \
  -metadata "title=To the Bone (Official Video)" \
  -metadata "artist=Pamungkas" \
  -metadata "album=Walk the Talk" \
  -metadata "date=2018" \
  -metadata "track=4" \
  -metadata "isrc=ID1234567890" \
  "$TAGGED_FLAC"

CAP_PROV="$TMP_DIR/providers-wp2b"
CAP_JSON="$TMP_DIR/provider-stdin.json"
mkdir -p "$CAP_PROV"
cat >"$CAP_PROV/10-cap.sh" <<CAP_EOF
#!/usr/bin/env bash
cat >"$CAP_JSON"
printf '{"url":"%s","codec":"flac","bit_depth":16,"sample_rate":44100}\n' "$TAGGED_FLAC"
CAP_EOF
chmod +x "$CAP_PROV/10-cap.sh"

assert_ok "play tagged flac in isolated mpv" "$BIN" play "$TAGGED_FLAC" >/dev/null
"$BIN" toggle >/dev/null
WP2B_STAT="$("$BIN" status --json)"
assert_ok "status --json exposes artist, album, year, track_num, isrc" jq -e '.artist == "Pamungkas" and .album == "Walk the Talk" and .year == 2018 and .track_num == 4 and .isrc == "ID1234567890"' <<<"$WP2B_STAT" >/dev/null

export MUSICCTL_PROVIDERS_DIR="$CAP_PROV"
rm -rf "${MUSICCTL_DOWNLOAD_DIR:?}"/*
DL_WP2B_CD="$("$BIN" dl --quality cd current)"
assert_contains "dl current cd uses capturing provider" "$DL_WP2B_CD" "10-cap.sh"
assert_ok "provider stdin receives album, isrc, track_num, year, and clean title" jq -e '.album == "Walk the Talk" and .isrc == "ID1234567890" and .track_num == 4 and .year == 2018 and .title == "To the Bone"' "$CAP_JSON" >/dev/null

OUT_WP2B_FLAC="$MUSICCTL_DOWNLOAD_DIR/Pamungkas/Walk the Talk/04 - To the Bone.flac"
assert_ok "dl current cd writes .flac file" test -s "$OUT_WP2B_FLAC"
FLAC_TAGS="$(ffprobe -v error -show_entries format_tags=title,artist,album,date,track,tracknumber,isrc -of json "$OUT_WP2B_FLAC")"
assert_ok "ffprobe confirms album/date/isrc written to .flac" jq -e '((.format.tags.album // .format.tags.ALBUM) == "Walk the Talk") and ((.format.tags.date // .format.tags.DATE) == "2018") and ((.format.tags.isrc // .format.tags.ISRC) == "ID1234567890")' <<<"$FLAC_TAGS" >/dev/null

DL_WP2B_SM="$("$BIN" dl --quality small --format m4a current)"
assert_contains "dl current small transcodes to AAC" "$DL_WP2B_SM" "aac"
OUT_WP2B_M4A="$MUSICCTL_DOWNLOAD_DIR/Pamungkas/Walk the Talk/04 - To the Bone.m4a"
assert_ok "dl current small writes .m4a file" test -s "$OUT_WP2B_M4A"
M4A_TAGS="$(ffprobe -v error -show_entries format_tags=title,artist,album,date,track -of json "$OUT_WP2B_M4A")"
assert_ok "ffprobe confirms album and date written to .m4a" jq -e '(.format.tags.album == "Walk the Talk") and (.format.tags.date == "2018")' <<<"$M4A_TAGS" >/dev/null
unset MUSICCTL_PROVIDERS_DIR
"$BIN" stop >/dev/null

# 13k. WP2C: Audiophile 774/251/141 format, cookies, soxr/ReplayGain, Hi-Res AF guard, SponsorBlock, and Autoplay
assert_contains "_ytm_format cd prioritizes 774/251/141" "$("$BIN" _ytm_format cd)" "774/251/141/bestaudio[acodec=opus]/bestaudio/best"
assert_contains "_ytm_format small uses <=96k" "$("$BIN" _ytm_format small)" "249/250/139"

AF_BASS_OFF="$("$BIN" _build_af_chain bass 0)"
assert_ok "norm=off preserves native sample rate (no aresample=48000)" bash -c '! grep -qF "aresample=48000" <<<"$1"' _ "$AF_BASS_OFF"
AF_NORM_ON="$("$BIN" _build_af_chain flat 1)"
assert_contains "norm=on includes loudnorm" "$AF_NORM_ON" "loudnorm="

ARGS_ALBUM="$(MUSICCTL_REPLAYGAIN=album "$BIN" _mpv_audiophile_args)"
assert_contains "replaygain album arg emitted" "$ARGS_ALBUM" "--replaygain=album"
assert_contains "soxr resampler arg emitted" "$ARGS_ALBUM" "resampler=soxr"
assert_contains "default ytdl-raw-options preserves ignore-config" "$ARGS_ALBUM" "ignore-config="

COOKIE_FILE="$TMP_DIR/cookies.txt"
printf '# Netscape HTTP Cookie File\n' >"$COOKIE_FILE"
ARGS_CK_FILE="$(MUSICCTL_YTM_COOKIES="$COOKIE_FILE" "$BIN" _mpv_audiophile_args)"
assert_contains "cookies file passed to ytdl-raw-options" "$ARGS_CK_FILE" "cookies=$COOKIE_FILE"
ARGS_CK_BR="$(MUSICCTL_YTM_COOKIES=firefox "$BIN" _mpv_audiophile_args)"
assert_contains "browser name passed as cookies-from-browser" "$ARGS_CK_BR" "cookies-from-browser=firefox"

SB_PARSED="$(printf '%s' '[{"segment":[10.0,22.0],"category":"music_offtopic"},{"segment":[30.0,30.2],"category":"music_offtopic"}]' | "$BIN" _parse_sponsorblock)"
assert_ok "offline SponsorBlock parser keeps >=0.5s music_offtopic" jq -e 'length == 1 and .[0].start == 10.0 and .[0].end == 22.0' <<<"$SB_PARSED" >/dev/null

AUTO_ON="$("$BIN" autoplay on)"
assert_contains "autoplay on succeeds" "$AUTO_ON" "Infinite autoplay: on"
assert_contains "autoplay status shows on" "$("$BIN" autoplay status)" "Infinite autoplay: on"
AUTO_STAT_JSON="$("$BIN" status --json)"
assert_ok "status --json includes autoplay true" jq -e '.autoplay == true and .sponsorblock == false' <<<"$AUTO_STAT_JSON" >/dev/null
AUTO_TGL="$("$BIN" autoplay toggle)"
assert_contains "autoplay toggle switches off" "$AUTO_TGL" "Infinite autoplay: off"
assert_ok "_autoplay_seed no-ops cleanly when autoplay=off" "$BIN" _autoplay_seed "IR7tYHdYN2Q"

# 13l. WP2D: Production Download Engine & Lazy Provider SSOT
PROV_DL="$TMP_DIR/providers-dl"
mkdir -p "$PROV_DL"
DL_SRC_FLAC="$TMP_DIR/dl_src.flac"
ffmpeg -y -v error -f lavfi -i "sine=frequency=523:duration=1" -ar 44100 -sample_fmt s16 -c:a flac "$DL_SRC_FLAC"
cat >"$PROV_DL/10-dl.sh" <<EOF
#!/usr/bin/env bash
cat >/dev/null
printf '{"provider":"10-dl.sh","url":"file://%s","codec":"flac","sample_rate":44100,"bit_depth":16}\n' "$DL_SRC_FLAC"
EOF
chmod +x "$PROV_DL/10-dl.sh"

export MUSICCTL_PROVIDERS_DIR="$PROV_DL"
rm -rf "${MUSICCTL_DOWNLOAD_DIR:?}"/* "$MUSICCTL_DATA_DIR/download-archive.tsv"

DL_OPUS="$("$BIN" dl --quality small "dQw4w9WgXcQ")"
assert_contains "dl --quality small defaults to opus" "$DL_OPUS" "opus"
assert_ok "dl --quality small writes .opus file" test -s "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.opus"
OPUS_CODEC="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nw=1:nk=1 "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.opus")"
assert_ok "small default .opus uses opus codec" test "$OPUS_CODEC" = "opus"

DL_MP3="$("$BIN" dl --quality small --format mp3 --replaygain "dQw4w9WgXcQ")"
assert_contains "dl --format mp3 writes mp3" "$DL_MP3" "mp3"
assert_ok "dl --format mp3 writes .mp3 file" test -s "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.mp3"
MP3_PROBE="$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name:format_tags -of json "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.mp3")"
assert_ok "dl --format mp3 --replaygain embeds REPLAYGAIN_TRACK_GAIN" jq -e '(.streams[0].codec_name == "mp3") and ((.format.tags | to_entries | map(.key | ascii_downcase)) | index("replaygain_track_gain") != null)' <<<"$MP3_PROBE" >/dev/null

DL_SKIP="$("$BIN" dl --quality small "dQw4w9WgXcQ")"
assert_contains "repeat dl skips via download-archive.tsv" "$DL_SKIP" "[SKIP]"
DL_FORCE="$("$BIN" dl --quality small --force "dQw4w9WgXcQ")"
assert_contains "dl --force re-downloads despite archive" "$DL_FORCE" "[OK] Downloaded"

DL_TPL_OUT="$("$BIN" dl --quality cd --template "custom/{title}" "dQw4w9WgXcQ")"
assert_contains "dl --template writes custom subpath" "$DL_TPL_OUT" "custom/dQw4w9WgXcQ.flac"
assert_ok "dl --template file exists on disk" test -s "$MUSICCTL_DOWNLOAD_DIR/custom/dQw4w9WgXcQ.flac"

rm -f "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
DL_DRY="$("$BIN" dl --quality cd --dry-run --force "dQw4w9WgXcQ")"
assert_contains "dl --dry-run reports planned path" "$DL_DRY" "[DRY-RUN]"
assert_ok "dl --dry-run does not create audio file" test ! -e "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"

# Test sync pl:<name> --prune --jobs 2 (scoped manifest prune preserves unrelated library files)
mkdir -p "$MUSICCTL_DATA_DIR/playlists"
rm -rf "${MUSICCTL_DOWNLOAD_DIR:?}"/* "$MUSICCTL_DATA_DIR/download-archive.tsv" "$MUSICCTL_DATA_DIR/sync-state.json"
printf 'trkA1111111\tSong Alpha\tArtist A • Album A • 0:01\ntrkB2222222\tSong Beta\tArtist B • Album B • 0:01\n' >"$MUSICCTL_DATA_DIR/playlists/wpsync.tsv"
printf 'unrelated audio\n' >"$MUSICCTL_DOWNLOAD_DIR/unrelated_library.flac"
"$BIN" dl --quality cd --jobs 2 sync pl:wpsync >/dev/null
assert_ok "dl sync fetches track 1 with default template" test -s "$MUSICCTL_DOWNLOAD_DIR/Artist A/Album A/Song Alpha.flac"
assert_ok "dl sync fetches track 2 with default template" test -s "$MUSICCTL_DOWNLOAD_DIR/Artist B/Album B/Song Beta.flac"
printf 'trkA1111111\tSong Alpha\tArtist A • Album A • 0:01\n' >"$MUSICCTL_DATA_DIR/playlists/wpsync.tsv"
SYNC_OUT="$("$BIN" dl --quality cd --jobs 2 sync pl:wpsync --prune)"
assert_contains "dl sync prints summary with pruned count" "$SYNC_OUT" "1 pruned"
assert_ok "dl sync prunes removed playlist track" test ! -e "$MUSICCTL_DOWNLOAD_DIR/Artist B/Album B/Song Beta.flac"
assert_ok "dl sync keeps retained playlist track" test -s "$MUSICCTL_DOWNLOAD_DIR/Artist A/Album A/Song Alpha.flac"
assert_ok "dl sync --prune preserves unrelated library file" test -s "$MUSICCTL_DOWNLOAD_DIR/unrelated_library.flac"
unset MUSICCTL_PROVIDERS_DIR

# 13m. WP2E: Local Library Indexer & Artist/Album Grouping
WP2E_LIB="$TMP_DIR/wp2e-lib"
WP2E_CACHE="$TMP_DIR/wp2e-cache"
mkdir -p "$WP2E_LIB" "$WP2E_CACHE"
ffmpeg -y -v error -f lavfi -i "sine=frequency=440:duration=1" -c:a flac \
  -metadata "artist=Pamungkas" -metadata "album=Walk the Talk" -metadata "title=To the Bone" -metadata "track=4" -metadata "date=2018" \
  "$WP2E_LIB/to_the_bone.flac"
ffmpeg -y -v error -f lavfi -i "sine=frequency=494:duration=1" -c:a flac \
  -metadata "artist=Pamungkas" -metadata "album=Walk the Talk" -metadata "title=Kenangan Manis" -metadata "track=2" -metadata "date=2018" \
  "$WP2E_LIB/kenangan_manis.flac"
ffmpeg -y -v error -f lavfi -i "sine=frequency=523:duration=1" -c:a aac \
  -metadata "artist=NIKI" -metadata "album=Nicole" -metadata "title=High School in Jakarta" -metadata "track=2" -metadata "date=2022" \
  "$WP2E_LIB/jakarta.m4a"

LIB_ARTIST_OUT="$(MUSICCTL_DOWNLOAD_DIR="$WP2E_LIB" MUSICCTL_CACHE_DIR="$WP2E_CACHE" "$BIN" local --by artist)"
assert_contains "local --by artist groups Pamungkas" "$LIB_ARTIST_OUT" "Pamungkas (2 tracks)"
assert_contains "local --by artist groups NIKI" "$LIB_ARTIST_OUT" "NIKI (1 tracks)"
assert_ok "local writes incremental library.json index" test -s "$WP2E_CACHE/library.json"

LIB_ALBUM_OUT="$(MUSICCTL_DOWNLOAD_DIR="$WP2E_LIB" MUSICCTL_CACHE_DIR="$WP2E_CACHE" "$BIN" local --by album)"
assert_contains "local --by album groups Walk the Talk" "$LIB_ALBUM_OUT" "Walk the Talk - Pamungkas (2 tracks)"
assert_contains "local --by album groups Nicole" "$LIB_ALBUM_OUT" "Nicole - NIKI (1 tracks)"

LIB_SEARCH_OUT="$(MUSICCTL_DOWNLOAD_DIR="$WP2E_LIB" MUSICCTL_CACHE_DIR="$WP2E_CACHE" "$BIN" local "jakarta")"
assert_contains "local query matches Jakarta" "$LIB_SEARCH_OUT" "High School in Jakarta"
assert_ok "local query excludes non-matching track" bash -c '! grep -qF "To the Bone" <<<"$1"' _ "$LIB_SEARCH_OUT"

LIB_JSON_OUT="$(MUSICCTL_DOWNLOAD_DIR="$WP2E_LIB" MUSICCTL_CACHE_DIR="$WP2E_CACHE" "$BIN" local --by flat --json)"
assert_ok "local --json outputs 3 indexed entries" jq -e 'length == 3 and ([.[].artist] | index("Pamungkas") != null)' <<<"$LIB_JSON_OUT" >/dev/null
assert_ok "local --rescan succeeds" env MUSICCTL_DOWNLOAD_DIR="$WP2E_LIB" MUSICCTL_CACHE_DIR="$WP2E_CACHE" "$BIN" local --rescan --by artist >/dev/null

# 13n. WP2F: DASH FLAC Manifest, OpenSubsonic Adapter & UNIX Lifecycle Hooks
DASH_DIR="$TMP_DIR/dash-src"
PROV_V2="$TMP_DIR/providers-v2"
HOOKS_V2="$TMP_DIR/hooks-v2"
mkdir -p "$DASH_DIR" "$PROV_V2" "$HOOKS_V2/on-download-complete.d" "$HOOKS_V2/on-track-change.d"
ffmpeg -y -v error -f lavfi -i "sine=frequency=440:duration=1" -ar 44100 -sample_fmt s16 -c:a flac -f dash "$DASH_DIR/manifest.mpd"

cat >"$PROV_V2/10-dash.sh" <<EOF
#!/usr/bin/env bash
cat >"$TMP_DIR/prov_v2_req.json"
printf '{"provider":"10-dash.sh","manifest":"file://%s/manifest.mpd","codec":"flac","sample_rate":44100,"bit_depth":16}\n' "$DASH_DIR"
EOF
chmod +x "$PROV_V2/10-dash.sh"

HOOK_DL_JSON="$TMP_DIR/hook_dl_payload.json"
cat >"$HOOKS_V2/on-download-complete.d/10-capture.sh" <<EOF
#!/usr/bin/env bash
cat >"$HOOK_DL_JSON"
EOF
cat >"$HOOKS_V2/on-download-complete.d/20-failing.sh" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
exit 1
EOF
chmod +x "$HOOKS_V2/on-download-complete.d/10-capture.sh" "$HOOKS_V2/on-download-complete.d/20-failing.sh"

HOOK_TRK_JSON="$TMP_DIR/hook_trk_payload.json"
cat >"$HOOKS_V2/on-track-change.d/10-track.sh" <<EOF
#!/usr/bin/env bash
cat >"$HOOK_TRK_JSON"
EOF
chmod +x "$HOOKS_V2/on-track-change.d/10-track.sh"

rm -rf "${MUSICCTL_DOWNLOAD_DIR:?}"/* "$MUSICCTL_DATA_DIR/download-archive.tsv"
DL_DASH_OUT="$(MUSICCTL_PROVIDERS_DIR="$PROV_V2" MUSICCTL_HOOKS_DIR="$HOOKS_V2" "$BIN" dl --quality cd --force "dQw4w9WgXcQ")"
assert_contains "dl remuxes DASH MPD provider to FLAC" "$DL_DASH_OUT" "10-dash.sh"
assert_ok "DASH remuxed .flac exists on disk" test -s "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac"
assert_ok "provider v2 stdin contains source and format" jq -e '.source == "ytm" and .format == "auto"' "$TMP_DIR/prov_v2_req.json" >/dev/null
assert_ok "DASH FLAC ffprobe verifies flac codec and duration" jq -e '(.streams[0].codec_name == "flac") and ((.format.duration | tonumber) > 0)' <<<"$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name:format=duration -of json "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac")" >/dev/null
assert_ok "on-download-complete hook received JSON despite 20-failing.sh exit 1" jq -e '.event == "on-download-complete" and .codec == "flac" and (.path | length > 0)' "$HOOK_DL_JSON" >/dev/null

# Verify OpenSubsonic provider script against local mock HTTP server
SUB_PORT_FILE="$TMP_DIR/subsonic.port"
python3 - "$SUB_PORT_FILE" "$TMP_DIR/subsonic.pid" <<'PY'
import http.server, json, os, sys
port_file, pid_file = sys.argv[1], sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "subsonic-response": {
                "status": "ok",
                "searchResult3": {
                    "song": [{
                        "id": "sub-99",
                        "title": "To the Bone",
                        "artist": "Pamungkas",
                        "suffix": "flac",
                        "samplingRate": 48000,
                        "bitDepth": 24,
                    }]
                },
            }
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H)
pid = os.fork()
if pid == 0:
    srv.serve_forever()
else:
    with open(port_file, "w") as f:
        f.write(str(srv.server_address[1]))
    with open(pid_file, "w") as f:
        f.write(str(pid))
PY
SUB_PORT="$(cat "$SUB_PORT_FILE")"
SUB_PID="$(cat "$TMP_DIR/subsonic.pid")"
SUB_OUT="$(printf '{"id":"t1","title":"To the Bone","artist":"Pamungkas","quality":"hires"}\n' | MUSICCTL_SUBSONIC_URL="http://127.0.0.1:$SUB_PORT" MUSICCTL_SUBSONIC_USER="alice" MUSICCTL_SUBSONIC_PASS="secret" "$ROOT_DIR/examples/providers/20-opensubsonic.sh")"
kill "$SUB_PID" 2>/dev/null || true
assert_ok "20-opensubsonic.sh returns valid lossless stream JSON" jq -e '.provider == "opensubsonic" and .codec == "flac" and .bit_depth == 24 and (.url | contains("rest/stream.view")) and (.url | contains("id=sub-99"))' <<<"$SUB_OUT" >/dev/null

# Verify on-track-change hook fires via mpv_hooks.lua during playback
"$BIN" stop >/dev/null 2>&1 || true
MUSICCTL_HOOKS_DIR="$HOOKS_V2" "$BIN" play "$MUSICCTL_DOWNLOAD_DIR/dQw4w9WgXcQ.flac" >/dev/null
for _ in $(seq 1 20); do
  [[ -s "$HOOK_TRK_JSON" ]] && break
  sleep 0.1
done
assert_ok "on-track-change hook fires from isolated mpv" jq -e '.event == "on-track-change" and (.path | contains("dQw4w9WgXcQ.flac"))' "$HOOK_TRK_JSON" >/dev/null
"$BIN" stop >/dev/null

# 13o. WP2G: JSONL Download Progress & Job Registry
JSONL_OUT="$(MUSICCTL_PROVIDERS_DIR="$PROV_DL" "$BIN" dl --quality small --jsonl --force "dQw4w9WgXcQ")"
JSONL_LAST="$(tail -n 1 <<<"$JSONL_OUT")"
assert_ok "dl --jsonl emits valid done JSONL event" jq -e '(.job_id | length > 0) and .status == "done" and .pct == 100' <<<"$JSONL_LAST" >/dev/null
DL_STAT_JSON="$("$BIN" dl status --json)"
assert_ok "dl status --json returns recorded jobs array" jq -e 'type == "array" and length >= 1' <<<"$DL_STAT_JSON" >/dev/null

# Gap closures: pl:<name> play/add & resolve-stream source forwarding
"$BIN" pl create gappl >/dev/null
printf '%s\tGap Song 1\tArtist G\n%s\tGap Song 2\tArtist G\n' "$WAV_FILE" "$WAV_FILE" >"$MUSICCTL_DATA_DIR/playlists/gappl.tsv"
"$BIN" play "pl:gappl" >/dev/null
"$BIN" add "pl:gappl" >/dev/null
Q_GAPPL="$("$BIN" queue | sed -E $'s/\x1b\\[[0-9;]*m//g')"
assert_contains "play+add pl:gappl queues 4 items" "$Q_GAPPL" "4. "
"$BIN" stop >/dev/null

# BUG-1 regression: _play_line routes catalog refs and _add_line routes pl:<name>
MUSICCTL_PROVIDERS_DIR="$PROV_STREAM" "$BIN" _play_line $'ytm:song:dQw4w9WgXcQ\tSong\tArtist\t[YTM]' >/dev/null
BUG1_STAT="$("$BIN" status --json)"
assert_ok "_play_line routes catalog ref through provider to mpv" jq -e '.state == "playing" and .codec == "pcm_s16le"' <<<"$BUG1_STAT" >/dev/null
"$BIN" _add_line $'pl:gappl\tgappl\t2 tracks\t[PL]' >/dev/null
Q_BUG1="$("$BIN" queue | sed -E $'s/\x1b\\[[0-9;]*m//g')"
assert_contains "_add_line pl:gappl appends playlist tracks" "$Q_BUG1" "3. "
"$BIN" stop >/dev/null

PROV_STRM_DIR="$TMP_DIR/prov-stream-src"
mkdir -p "$PROV_STRM_DIR"
STRM_REQ="$TMP_DIR/stream_src_req.json"
cat >"$PROV_STRM_DIR/10-stream.sh" <<EOF
#!/usr/bin/env bash
cat >"$STRM_REQ"
printf '{"provider":"10-stream.sh","url":"file://%s","codec":"wav"}\n' "$WAV_FILE"
EOF
chmod +x "$PROV_STRM_DIR/10-stream.sh"
MUSICCTL_PROVIDERS_DIR="$PROV_STRM_DIR" python3 "$ROOT_DIR/lib/providers.py" resolve-stream cd trk9 "Title" "Artist" "Album" 10 US123 1 2024 deezer >/dev/null
assert_ok "resolve-stream forwards trailing source=deezer" jq -e '.source == "deezer"' "$STRM_REQ" >/dev/null

# 14. Live network smoke tests (skipped if MUSICCTL_OFFLINE_TESTS=1)
if [[ "$OFFLINE" != "1" ]]; then
  SEARCH_OUT="$("$BIN" search "pamungkas to the bone")"
  assert_contains "live YTM search returns To the Bone" "$SEARCH_OUT" "IR7tYHdYN2Q"

  LYRICS_LIVE="$("$BIN" lyrics "Pamungkas To the Bone")"
  assert_contains "live LRCLIB lyrics returns To the Bone" "$LYRICS_LIVE" "Pamungkas"

  MPV_OPUS="$(mpv --no-video --ao=null --end=0.1 --ytdl-format='251/bestaudio[acodec=opus]/bestaudio/best' --ytdl-raw-options='ignore-config=,no-playlist=,no-update=' 'https://www.youtube.com/watch?v=IR7tYHdYN2Q' 2>&1 || true)"
  assert_contains "live mpv resolves Opus 48kHz audio" "$MPV_OPUS" "opus 2ch 48000 Hz"
fi

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
((FAIL == 0))
