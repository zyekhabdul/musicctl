#!/usr/bin/env bash
set -uo pipefail

BIN="/home/aomiqaza/Projects/musicctl/bin/musicctl"
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

# 10. Version, doctor, config precedence, synced lyrics JSON, and auto-cache resolution
VER_OUT="$("$BIN" --version)"
assert_contains "version prints 1.0.0" "$VER_OUT" "musicctl 1.0.0"

DOC_OUT="$("$BIN" doctor)"
assert_contains "doctor checks mpv" "$DOC_OUT" "mpv"
assert_contains "doctor checks chafa" "$DOC_OUT" "chafa"

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
    w.writeframes(b"\x00\x00" * 16000)
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
"$BIN" stop >/dev/null

# 12. Desktop entry validation & install.sh dry-install/uninstall
if command -v desktop-file-validate >/dev/null 2>&1; then
  assert_ok "desktop-file-validate musicctl.desktop" desktop-file-validate "/home/aomiqaza/Projects/musicctl/musicctl.desktop"
fi
PREFIX_TMP="$TMP_DIR/prefix"
assert_ok "install.sh dry install" /home/aomiqaza/Projects/musicctl/install.sh --prefix "$PREFIX_TMP" >/dev/null
assert_ok "installed bin exists" test -x "$PREFIX_TMP/bin/musicctl"
assert_ok "installed desktop exists" test -f "$PREFIX_TMP/share/applications/musicctl.desktop"
assert_ok "install.sh uninstall" /home/aomiqaza/Projects/musicctl/install.sh --prefix "$PREFIX_TMP" --uninstall >/dev/null
assert_ok "installed bin removed" test ! -e "$PREFIX_TMP/bin/musicctl"

# 13. Live network smoke tests (skipped if MUSICCTL_OFFLINE_TESTS=1)
if [[ "$OFFLINE" != "1" ]]; then
  SEARCH_OUT="$("$BIN" search "pamungkas to the bone")"
  assert_contains "live YTM search returns To the Bone" "$SEARCH_OUT" "IR7tYHdYN2Q"

  LYRICS_LIVE="$("$BIN" lyrics "Pamungkas To the Bone")"
  assert_contains "live LRCLIB lyrics returns To the Bone" "$LYRICS_LIVE" "Pamungkas"
fi

printf '\n=== SUMMARY: %d passed, %d failed ===\n' "$PASS" "$FAIL"
((FAIL == 0))
