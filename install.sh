#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HOME}/.local"
UNINSTALL=0

usage() {
  cat <<EOF
Usage: ./install.sh [--prefix <dir>] [--uninstall]
EOF
}

while (($# > 0)); do
  case "$1" in
    --prefix)
      PREFIX="${2:?Missing path for --prefix}"
      shift 2
      ;;
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERR] Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

BIN_DIR="${PREFIX}/bin"
DATA_DIR="${PREFIX}/share"
if [[ "$PREFIX" == "${HOME}/.local" ]]; then
  DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
fi

BASH_COMP="${DATA_DIR}/bash-completion/completions/musicctl"
ZSH_COMP="${DATA_DIR}/zsh/site-functions/_musicctl"
DESKTOP_DST="${DATA_DIR}/applications/musicctl.desktop"
COMMON_DST="${DATA_DIR}/musicctl/common.py"
TUI_DST="${DATA_DIR}/musicctl/tui.py"

if ((UNINSTALL == 1)); then
  rm -f "${BIN_DIR}/musicctl" "$BASH_COMP" "$ZSH_COMP" "$DESKTOP_DST" "$COMMON_DST" "$TUI_DST"
  rmdir "${DATA_DIR}/musicctl" 2>/dev/null || true
  echo "[OK] Uninstalled musicctl from ${PREFIX}"
  exit 0
fi

install -Dm755 "${SCRIPT_DIR}/bin/musicctl" "${BIN_DIR}/musicctl"
install -Dm644 "${SCRIPT_DIR}/lib/common.py" "$COMMON_DST"
install -Dm644 "${SCRIPT_DIR}/lib/tui.py" "$TUI_DST"
install -Dm644 "${SCRIPT_DIR}/completions/musicctl.bash" "$BASH_COMP"
install -Dm644 "${SCRIPT_DIR}/completions/_musicctl" "$ZSH_COMP"
install -Dm644 "${SCRIPT_DIR}/musicctl.desktop" "$DESKTOP_DST"

if [[ "$PREFIX" != "/usr" && "$PREFIX" != "/usr/local" ]]; then
  sed -i "s|^Exec=.*|Exec=${BIN_DIR}/musicctl|" "$DESKTOP_DST"
fi

echo "[OK] Installed musicctl -> ${BIN_DIR}/musicctl"
echo "[OK] TUI module -> ${TUI_DST}"
echo "[OK] Completions -> ${BASH_COMP}, ${ZSH_COMP}"
echo "[OK] Desktop entry -> ${DESKTOP_DST}"

missing=()
for dep in mpv yt-dlp fzf jq curl python3; do
  command -v "$dep" >/dev/null 2>&1 || missing+=("$dep")
done
if ((${#missing[@]} > 0)); then
  echo "[WARN] Missing runtime dependencies: ${missing[*]}"
else
  echo "[OK] All core runtime dependencies present"
fi
