#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_FILE="${HOME}/.local/share/applications/minicam.desktop"

mkdir -p "${HOME}/.local/share/applications"

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=MINICAM
Comment=Camera app
Exec=${APP_DIR}/.venv/bin/python ${APP_DIR}/app.py
Path=${APP_DIR}
Icon=${APP_DIR}/logo.svg
Terminal=false
Categories=Utility;
EOF

chmod +x "${DESKTOP_FILE}"

if [ -d "${HOME}/Desktop" ]; then
  cp "${DESKTOP_FILE}" "${HOME}/Desktop/minicam.desktop"
  chmod +x "${HOME}/Desktop/minicam.desktop"
fi

echo "Created launcher at ${DESKTOP_FILE}"
echo "Desktop shortcut added if ~/Desktop exists."
