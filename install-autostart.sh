#!/bin/bash
# Instala el refresh server como LaunchAgent (auto-arranca con el login de Mac)
PLIST="$HOME/Library/LaunchAgents/com.latam-ps.dashboard-server.plist"
PYTHON=$(which python3)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cat > "$PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.latam-ps.dashboard-server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT_DIR/refresh-server.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/server.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/server.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
echo "✅ LaunchAgent instalado. El servidor arrancará automáticamente en cada login."
echo "   Para verificar: curl http://localhost:3001/health"
