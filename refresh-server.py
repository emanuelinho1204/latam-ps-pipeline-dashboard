#!/usr/bin/env python3
"""
LATAM PS Dashboard — Local Refresh Server
Run once: python3 refresh-server.py
Then the "Update data" button in the dashboard will work.
"""

import http.server
import subprocess
import json
import os
import sys
import threading
from datetime import datetime

PORT = 3001
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

refresh_status = {"running": False, "last": None, "log": []}

def run_refresh():
    refresh_status["running"] = True
    refresh_status["log"] = []
    refresh_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando refresh de datos...")

    claude_bin = os.path.expanduser("~/.local/bin/claude")
    if not os.path.exists(claude_bin):
        claude_bin = "claude"

    cmd = [claude_bin, "-p", "@Claude ps-dashboard", "--no-streaming"]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=DASHBOARD_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                refresh_status["log"].append(line)
        proc.wait()
        if proc.returncode == 0:
            refresh_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Dashboard actualizado exitosamente.")
            refresh_status["last"] = datetime.now().isoformat()
        else:
            refresh_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Proceso terminó con código {proc.returncode}")
    except Exception as e:
        refresh_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error: {str(e)}")
    finally:
        refresh_status["running"] = False


class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            self._json(refresh_status)
        elif self.path == "/health":
            self._json({"ok": True, "port": PORT})
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def do_POST(self):
        if self.path == "/refresh":
            if refresh_status["running"]:
                self._json({"ok": False, "message": "Refresh ya en progreso..."})
            else:
                t = threading.Thread(target=run_refresh, daemon=True)
                t.start()
                self._json({"ok": True, "message": "Refresh iniciado"})
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass  # suppress default HTTP logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print(f"🔄 LATAM PS Dashboard Refresh Server")
    print(f"   Listening on http://localhost:{PORT}")
    print(f"   Dashboard: {DASHBOARD_DIR}/index.html")
    print(f"   Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
