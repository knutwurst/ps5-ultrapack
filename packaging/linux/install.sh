#!/usr/bin/env bash
# adds PS5 UltraPack to the app menu for this user; run it from the unpacked folder
# --uninstall takes the menu entry and icon away again (the folder stays where it is)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-${HOME}/.local/share}"
DESKTOP="${DATA}/applications/ps5-ultrapack.desktop"
ICON="${DATA}/icons/hicolor/256x256/apps/ps5-ultrapack.png"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "${DESKTOP}" "${ICON}"
    echo "Removed the menu entry."
    exit 0
fi

install -Dm 644 "${HERE}/ps5-ultrapack.png" "${ICON}"
mkdir -p "$(dirname "${DESKTOP}")"
cat > "${DESKTOP}" <<EOF
[Desktop Entry]
Type=Application
Name=PS5 UltraPack
Comment=Pack, convert, patch and sign PS5 games
Exec="${HERE}/PS5 UltraPack" %F
Icon=ps5-ultrapack
Terminal=false
Categories=Utility;Archiving;
StartupWMClass=PS5 UltraPack
EOF
command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "${DESKTOP}")" || true
echo "Added PS5 UltraPack to the app menu (${DESKTOP})."
