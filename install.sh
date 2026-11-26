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
LIB_DST_DIR="${PREFIX}/lib/musicctl"
LEGACY_LIB_DIR="${DATA_DIR}/musicctl"
LIB_FILES=(common.py catalog.py providers.py downloader.py library.py tui_widgets.py tui.py mpv_hooks.lua)

if ((UNINSTALL == 1)); then
  rm -f "${BIN_DIR}/musicctl" "$BASH_COMP" "$ZSH_COMP" "$DESKTOP_DST"
  for lf in "${LIB_FILES[@]}"; do
    rm -f "${LIB_DST_DIR}/${lf}"
  done
  rm -rf "${LIB_DST_DIR}/__pycache__"
  rmdir "$LIB_DST_DIR" 2>/dev/null || true
  if [[ "$LEGACY_LIB_DIR" != "$LIB_DST_DIR" && -d "$LEGACY_LIB_DIR" ]]; then
    for lf in "${LIB_FILES[@]}"; do
      rm -f "${LEGACY_LIB_DIR}/${lf}"
    done
    rm -rf "${LEGACY_LIB_DIR}/__pycache__"
    rmdir "$LEGACY_LIB_DIR" 2>/dev/null || true
  fi
  echo "[OK] Uninstalled musicctl from ${PREFIX}"
  exit 0
fi

install -Dm755 "${SCRIPT_DIR}/bin/musicctl" "${BIN_DIR}/musicctl"
for lf in "${LIB_FILES[@]}"; do
  install -Dm644 "${SCRIPT_DIR}/lib/${lf}" "${LIB_DST_DIR}/${lf}"
done
if [[ "$LEGACY_LIB_DIR" != "$LIB_DST_DIR" && -d "$LEGACY_LIB_DIR" ]]; then
  for lf in "${LIB_FILES[@]}"; do
    rm -f "${LEGACY_LIB_DIR}/${lf}"
  done
  rm -rf "${LEGACY_LIB_DIR}/__pycache__"
fi
install -Dm644 "${SCRIPT_DIR}/completions/musicctl.bash" "$BASH_COMP"
install -Dm644 "${SCRIPT_DIR}/completions/_musicctl" "$ZSH_COMP"
install -Dm644 "${SCRIPT_DIR}/musicctl.desktop" "$DESKTOP_DST"

if [[ "$PREFIX" != "/usr" && "$PREFIX" != "/usr/local" ]]; then
  sed -i "s|^Exec=.*|Exec=${BIN_DIR}/musicctl|" "$DESKTOP_DST"
fi

echo "[OK] Installed musicctl -> ${BIN_DIR}/musicctl"
echo "[OK] Runtime modules -> ${LIB_DST_DIR}/"
echo "[OK] Completions -> ${BASH_COMP}, ${ZSH_COMP}"
echo "[OK] Desktop entry -> ${DESKTOP_DST}"

missing=()
for dep in mpv yt-dlp jq curl python3; do
  command -v "$dep" >/dev/null 2>&1 || missing+=("$dep")
done
if ((${#missing[@]} > 0)); then
  echo "[WARN] Missing runtime dependencies: ${missing[*]}"
else
  echo "[OK] All core runtime dependencies present"
fi
