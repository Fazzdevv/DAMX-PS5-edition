#!/bin/bash
# ==============================================================================
# DAMX PS5 Edition - 1-Click Quick Installer
# Compatible with: Arch, Ubuntu/Debian, Fedora, openSUSE, CachyOS, Nobara, etc.
# ==============================================================================

set -e

# ANSI Color Codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}====================================================${NC}"
echo -e "${GREEN}     DAMX Suite (PS5 Fan Edition) - Quick Installer   ${NC}"
echo -e "${BLUE}====================================================${NC}"
echo ""

# 1. Check Root Privileges
if [ "$EUID" -ne 0 ]; then
  echo -e "${YELLOW}Script ini memerlukan hak akses root. Menjalankan dengan sudo...${NC}"
  exec sudo bash "$0" "$@"
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

INSTALL_DIR="/opt/damx"
SYSTEMD_DIR="/etc/systemd/system"
DAEMON_SERVICE="damx-daemon.service"
BIN_DIR="/usr/local/bin"
DESKTOP_FILE_DIR="/usr/share/applications"
ICON_DIR="/usr/share/icons/hicolor/256x256/apps"

# 2. Check and Build / Locate Binaries
echo -e "${YELLOW}[1/4] Memeriksa binary aplikasi...${NC}"

# Locate or build Daemon
DAEMON_BIN=""
if [ -f "$DIR/DAMX-Daemon/DAMX-Daemon" ]; then
  DAEMON_BIN="$DIR/DAMX-Daemon/DAMX-Daemon"
elif [ -f "$DIR/DAMM-Daemon/dist/DAMX-Daemon" ]; then
  DAEMON_BIN="$DIR/DAMM-Daemon/dist/DAMX-Daemon"
elif [ -f "$DIR/DAMM-Daemon/DAMX-Daemon.py" ]; then
  echo -e "${BLUE}Membangun binary Daemon dengan PyInstaller...${NC}"
  (cd "$DIR/DAMM-Daemon" && pyinstaller --onefile --clean DAMX-Daemon.py)
  DAEMON_BIN="$DIR/DAMM-Daemon/dist/DAMX-Daemon"
fi

# Locate or build GUI
GUI_BIN=""
if [ -f "$DIR/DAMX-GUI/DivAcerManagerMax" ]; then
  GUI_BIN="$DIR/DAMX-GUI/DivAcerManagerMax"
elif [ -f "$DIR/DivAcerManagerMax/bin/Release/net9.0/linux-x64/publish/DivAcerManagerMax" ]; then
  GUI_BIN="$DIR/DivAcerManagerMax/bin/Release/net9.0/linux-x64/publish/DivAcerManagerMax"
elif [ -f "$DIR/DivAcerManagerMax/DivAcerManagerMax.csproj" ]; then
  echo -e "${BLUE}Membangun GUI dengan dotnet publish...${NC}"
  DOTNET_CMD="dotnet"
  if ! command -v dotnet &>/dev/null && [ -f "$HOME/.dotnet/dotnet" ]; then
    DOTNET_CMD="$HOME/.dotnet/dotnet"
  fi
  (cd "$DIR/DivAcerManagerMax" && "$DOTNET_CMD" publish -c Release -f net9.0 -r linux-x64 --self-contained true /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true /p:IncludeAllContentForSelfExtract=true)
  GUI_BIN="$DIR/DivAcerManagerMax/bin/Release/net9.0/linux-x64/publish/DivAcerManagerMax"
fi

if [ -z "$DAEMON_BIN" ] || [ ! -f "$DAEMON_BIN" ]; then
  echo -e "${RED}Error: Binary DAMX-Daemon tidak ditemukan atau gagal di-build.${NC}"
  exit 1
fi

if [ -z "$GUI_BIN" ] || [ ! -f "$GUI_BIN" ]; then
  echo -e "${RED}Error: Binary DivAcerManagerMax tidak ditemukan atau gagal di-build.${NC}"
  exit 1
fi

# 3. Build & Install Linuwu-Sense Driver (if present)
echo -e "${YELLOW}[2/4] Memeriksa driver kernel Linuwu-Sense...${NC}"
if [ -d "$DIR/Linuwu-Sense" ]; then
  echo -e "${BLUE}Mengompilasi dan memasang kernel driver Linuwu-Sense...${NC}"
  cd "$DIR/Linuwu-Sense"
  make clean >/dev/null 2>&1 || true
  make -j"$(nproc)"
  make install
  cd "$DIR"
  echo -e "${GREEN}✓ Driver Linuwu-Sense berhasil dipasang!${NC}"
else
  echo -e "${BLUE}Direktori driver Linuwu-Sense tidak ditemukan di folder ini, menggunakan driver kernel yang sudah ada di sistem.${NC}"
fi

# 4. Install DAMX-Daemon
echo -e "${YELLOW}[3/4] Memasang DAMX-Daemon (PS5 Adaptive Fan Service)...${NC}"
mkdir -p "$INSTALL_DIR/daemon"
cp -f "$DAEMON_BIN" "$INSTALL_DIR/daemon/DAMX-Daemon"
chmod +x "$INSTALL_DIR/daemon/DAMX-Daemon"

# Create systemd service
cat > "$SYSTEMD_DIR/$DAEMON_SERVICE" << 'EOF'
[Unit]
Description=DAMX Daemon for Acer laptops
After=network.target

[Service]
Type=simple
ExecStart=/opt/damx/daemon/DAMX-Daemon
Restart=on-failure
RestartSec=5
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$DAEMON_SERVICE"
systemctl restart "$DAEMON_SERVICE"

# 5. Install DAMX-GUI
echo -e "${YELLOW}[4/4] Memasang DAMX-GUI & Menu Launcher...${NC}"
mkdir -p "$INSTALL_DIR/gui"
mkdir -p "$ICON_DIR"
mkdir -p "$BIN_DIR"
mkdir -p "$DESKTOP_FILE_DIR"

cp -f "$GUI_BIN" "$INSTALL_DIR/gui/DivAcerManagerMax"
chmod +x "$INSTALL_DIR/gui/DivAcerManagerMax"

# Copy icons
if [ -f "$DIR/DAMX-GUI/icon.png" ]; then
  cp -f "$DIR/DAMX-GUI/icon.png" "$INSTALL_DIR/gui/icon.png"
  cp -f "$DIR/DAMX-GUI/icon.png" "$ICON_DIR/damx.png"
elif [ -f "$DIR/DivAcerManagerMax/icon.png" ]; then
  cp -f "$DIR/DivAcerManagerMax/icon.png" "$INSTALL_DIR/gui/icon.png"
  cp -f "$DIR/DivAcerManagerMax/icon.png" "$ICON_DIR/damx.png"
fi

# Shortcut CLI
cat > "$BIN_DIR/DAMX" << 'EOF'
#!/bin/bash
/opt/damx/gui/DivAcerManagerMax "$@"
EOF
chmod +x "$BIN_DIR/DAMX"

# Shortcut Menu Aplikasi (.desktop)
cat > "$DESKTOP_FILE_DIR/damx.desktop" << 'EOF'
[Desktop Entry]
Name=DAMX
Comment=Div Acer Manager Max (PS5 Fan Edition)
Exec=/opt/damx/gui/DivAcerManagerMax
Icon=damx
Terminal=false
Type=Application
Categories=Utility;System;
Keywords=acer;laptop;system;nitro;predator;
EOF

echo ""
echo -e "${GREEN}====================================================${NC}"
echo -e "${GREEN}       INSTALLASI DAMX PS5 EDITION BERHASIL!        ${NC}"
echo -e "${GREEN}====================================================${NC}"
echo -e "Aplikasi sekarang sudah terpasang di sistem kamu."
echo -e "Kamu bisa membukanya melalui:"
echo -e "  1. Buka ${YELLOW}Menu Aplikasi / App Launcher${NC} -> cari ${YELLOW}DAMX${NC}"
echo -e "  2. Atau ketik ${YELLOW}DAMX${NC} di terminal"
echo ""
echo -e "Status Daemon:"
systemctl status "$DAEMON_SERVICE" --no-pager -l | head -n 8 || true
echo ""
