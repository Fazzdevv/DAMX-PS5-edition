#!/bin/bash
set -e

# Pastikan dijalankan sebagai root / sudo
if [ "$EUID" -ne 0 ]; then
  echo "Script ini membutuhkan hak akses root. Menjalankan dengan sudo..."
  exec sudo bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/damx"
SYSTEMD_DIR="/etc/systemd/system"
DAEMON_SERVICE="damx-daemon.service"
BIN_DIR="/usr/local/bin"
DESKTOP_FILE_DIR="/usr/share/applications"
ICON_DIR="/usr/share/icons/hicolor/256x256/apps"

echo "=========================================="
echo "    DAMX Custom Installer (PS5 Mode)      "
echo "=========================================="

DAEMON_BIN="$SCRIPT_DIR/DAMM-Daemon/dist/DAMX-Daemon"
GUI_BIN="$SCRIPT_DIR/DivAcerManagerMax/bin/Release/net9.0/linux-x64/publish/DivAcerManagerMax"

# 1. Cek ketersediaan binary
if [ ! -f "$DAEMON_BIN" ]; then
  echo "Error: Binary DAMX-Daemon belum di-build!"
  echo "Jalankan build daemon terlebih dahulu:"
  echo "  cd $SCRIPT_DIR/DAMM-Daemon && pyinstaller --onefile --clean DAMX-Daemon.py"
  exit 1
fi

if [ ! -f "$GUI_BIN" ]; then
  echo "Error: Binary DivAcerManagerMax belum di-build!"
  echo "Jalankan build GUI terlebih dahulu:"
  echo "  cd $SCRIPT_DIR/DivAcerManagerMax && ~/.dotnet/dotnet publish -c Release -f net9.0 -r linux-x64 --self-contained true /p:PublishSingleFile=true"
  exit 1
fi

# 2. Hentikan service jika sedang berjalan
if systemctl is-active --quiet $DAEMON_SERVICE 2>/dev/null; then
  echo "Menghentikan $DAEMON_SERVICE..."
  systemctl stop $DAEMON_SERVICE
fi

# 3. Buat direktori tujuan
mkdir -p "$INSTALL_DIR/daemon"
mkdir -p "$INSTALL_DIR/gui"
mkdir -p "$ICON_DIR"

# 4. Salin Daemon baru
echo "Memasang DAMX-Daemon ke $INSTALL_DIR/daemon/..."
cp -f "$DAEMON_BIN" "$INSTALL_DIR/daemon/DAMX-Daemon"
chmod +x "$INSTALL_DIR/daemon/DAMX-Daemon"

# 5. Salin GUI baru & Icons
echo "Memasang DAMX-GUI ke $INSTALL_DIR/gui/..."
cp -f "$GUI_BIN" "$INSTALL_DIR/gui/DivAcerManagerMax"
chmod +x "$INSTALL_DIR/gui/DivAcerManagerMax"

if [ -f "$SCRIPT_DIR/DivAcerManagerMax/icon.png" ]; then
  cp -f "$SCRIPT_DIR/DivAcerManagerMax/icon.png" "$INSTALL_DIR/gui/icon.png"
  cp -f "$SCRIPT_DIR/DivAcerManagerMax/icon.png" "$ICON_DIR/damx.png"
fi

# 6. Pastikan shortcut terminal /usr/local/bin/DAMX
cat > "$BIN_DIR/DAMX" << 'EOL'
#!/bin/bash
/opt/damx/gui/DivAcerManagerMax "$@"
EOL
chmod +x "$BIN_DIR/DAMX"

# 7. Pastikan shortcut launcher aplikasi (.desktop)
cat > "$DESKTOP_FILE_DIR/damx.desktop" << 'EOL'
[Desktop Entry]
Name=DAMX
Comment=Div Acer Manager Max (Custom PS5 Fan Mode)
Exec=/opt/damx/gui/DivAcerManagerMax
Icon=damx
Terminal=false
Type=Application
Categories=Utility;System;
Keywords=acer;laptop;system;
EOL

# 8. Pastikan systemd unit file terpasang
cat > "$SYSTEMD_DIR/$DAEMON_SERVICE" << 'EOL'
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
EOL

# 9. Reload systemd & nyalakan daemon
echo "Me-reload systemd dan menyalakan $DAEMON_SERVICE..."
systemctl daemon-reload
systemctl enable $DAEMON_SERVICE
systemctl start $DAEMON_SERVICE

if systemctl is-active --quiet $DAEMON_SERVICE; then
  echo "------------------------------------------"
  echo " Berhasil! DAMX dengan PS5 Adaptive Fan   "
  echo " sudah terpasang dan aktif di sistem.     "
  echo "------------------------------------------"
  echo "Kamu sekarang bisa membuka aplikasi DAMX langsung"
  echo "dari Menu Aplikasi / App Launcher desktop kamu."
else
  echo "Warning: Service tidak berjalan sempurna. Cek dengan:"
  echo "  sudo journalctl -u damx-daemon -n 50"
fi
