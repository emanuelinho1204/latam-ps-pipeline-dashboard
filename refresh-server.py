#!/usr/bin/env python3
"""
LATAM PS Dashboard — Local Refresh Server
Run once: python3 refresh-server.py
Fetches live org62 data and serves it to the dashboard via CORS-accessible endpoints.
"""

import http.server
import subprocess
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

PORT = 3001
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
SF_BIN = os.path.expanduser("~/.aisuite/bin/sf")

# In-memory cache: 5-minute TTL
_cache = {"data": None, "ts": 0, "ttl": 300}
_lock = threading.Lock()

refresh_status = {"running": False, "last": None, "log": []}


def ts():
    return datetime.now().strftime('%H:%M:%S')


def soql(query):
    """Run a SOQL query via SF CLI and return records list."""
    result = subprocess.run(
        [SF_BIN, "data", "query", "--target-org", "org62", "--query", query, "--json"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"SF CLI error: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    return data.get("result", {}).get("records", [])


def fetch_live_data():
    """Fetch DSR_DATA and ESTIMATE_MAP directly from org62 via SF CLI."""
    log = []
    log.append(f"[{ts()}] Consultando DSRs en org62...")

    dsr_records = soql(
        "SELECT Id, Name, Owner.Name, Status__c, "
        "Opportunity__r.Name, Opportunity__r.Amount, Opportunity__r.CloseDate, "
        "Opportunity__r.Owner.Name, Request_Type__c, Opportunity__r.ForecastCategoryName, "
        "Opportunity__r.NextStep, Region__c, Request_Type_Detail__c, "
        "Opportunity__r.Account.Name, Opportunity__r.Account.BillingCountry, "
        "Opportunity__r.Manager_Forecast_Judgement__c, Opportunity__r.ContractType__c, "
        "Opportunity__r.StageName, Opportunity__c "
        "FROM Deal_Support_Request__c "
        "WHERE RecordType.Name = 'CSG Professional Services Deal Support' "
        "AND Status__c NOT IN ('Closed - Resolved', 'Closed - No Response', 'Closed - Duplicate', 'Closed - Not Qualified') "
        "AND Request_Type__c IN ('EM Support', 'Admin Support', 'SOW Admin Support') "
        "AND Opportunity__r.StageName NOT IN ('06 - Project Booked', 'Dead - Lost') "
        "AND (Region__c = 'LACA' OR Region__c = 'LATAM') "
        "AND Request_Type_Detail__c IN ('Deal Strategy', 'RFP', 'Scoping/SOW', 'SOW / SPW Generation') "
        "AND Opportunity__r.CloseDate >= 2026-02-01 "
        "AND Opportunity__r.CloseDate <= 2028-01-31 "
        "ORDER BY Owner.Name, Opportunity__r.StageName, Status__c "
        "LIMIT 250"
    )

    log.append(f"[{ts()}] {len(dsr_records)} DSRs obtenidos. Procesando...")

    dsr_data = []
    opp_ids = set()
    for r in dsr_records:
        opp = r.get("Opportunity__r") or {}
        opp_id = r.get("Opportunity__c")
        if opp_id:
            opp_ids.add(opp_id)
        dsr_data.append({
            "dsrId":            r["Id"],
            "dsr":              r.get("Name", ""),
            "sssm":             (r.get("Owner") or {}).get("Name", ""),
            "status":           r.get("Status__c", ""),
            "oppName":          opp.get("Name", ""),
            "oppId":            opp_id or "",
            "amount":           opp.get("Amount") or 0,
            "closeDate":        opp.get("CloseDate", ""),
            "ae":               (opp.get("Owner") or {}).get("Name", ""),
            "ap":               (opp.get("Owner") or {}).get("Name", ""),
            "requestType":      r.get("Request_Type__c", ""),
            "forecast":         opp.get("ForecastCategoryName", ""),
            "nextStep":         opp.get("NextStep", ""),
            "region":           r.get("Region__c", ""),
            "requestTypeDetail": r.get("Request_Type_Detail__c", ""),
            "account":          (opp.get("Account") or {}).get("Name", ""),
            "country":          (opp.get("Account") or {}).get("BillingCountry", ""),
            "mgrJudgement":     opp.get("Manager_Forecast_Judgement__c"),
            "contractType":     opp.get("ContractType__c"),
            "stage":            opp.get("StageName", ""),
        })

    # Fetch primary estimates in batches of 90
    estimate_map = {}
    if opp_ids:
        opp_list = list(opp_ids)
        log.append(f"[{ts()}] Consultando estimates para {len(opp_list)} opps...")
        for i in range(0, len(opp_list), 90):
            batch = opp_list[i:i+90]
            ids_str = "','".join(batch)
            est_records = soql(
                f"SELECT Id, Name, ffscpq__Billing_Type__c, ffscpq__Opportunity__c "
                f"FROM ffscpq__Estimate__c "
                f"WHERE ffscpq__Is_Primary__c = true "
                f"AND ffscpq__Opportunity__c IN ('{ids_str}') "
                f"LIMIT 100"
            )
            for e in est_records:
                opp_id = e.get("ffscpq__Opportunity__c")
                if opp_id:
                    estimate_map[opp_id] = {
                        "id":          e["Id"],
                        "name":        e.get("Name", ""),
                        "billingType": e.get("ffscpq__Billing_Type__c", ""),
                    }
        log.append(f"[{ts()}] {len(estimate_map)} estimates obtenidos.")

    log.append(f"[{ts()}] ✅ Datos frescos listos — {len(dsr_data)} DSRs, {len(estimate_map)} estimates.")
    return {
        "dsrData":     dsr_data,
        "estimateMap": estimate_map,
        "generated":   datetime.now().strftime("%Y-%m-%d"),
        "log":         log,
    }


def get_data(force=False):
    """Return cached data or fetch fresh if expired."""
    with _lock:
        if not force and _cache["data"] and (time.time() - _cache["ts"] < _cache["ttl"]):
            return _cache["data"]
    data = fetch_live_data()
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()
    return data


def run_refresh():
    """Fetch fresh data and update index.html constants in-place."""
    refresh_status["running"] = True
    refresh_status["log"] = []
    refresh_status["log"].append(f"[{ts()}] Iniciando refresh de datos...")

    try:
        data = get_data(force=True)
        refresh_status["log"].extend(data.get("log", []))

        idx_path = os.path.join(DASHBOARD_DIR, "index.html")
        with open(idx_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Replace let DSR_DATA = [...]
        dsr_json = json.dumps(data["dsrData"], ensure_ascii=False, indent=2)
        content = re.sub(
            r'let DSR_DATA = \[.*?\];',
            f'let DSR_DATA = {dsr_json};',
            content, flags=re.DOTALL
        )

        # Replace let ESTIMATE_MAP = {...}
        est_json = json.dumps(data["estimateMap"], ensure_ascii=False, indent=2)
        content = re.sub(
            r'let ESTIMATE_MAP = \{.*?\};',
            f'let ESTIMATE_MAP = {est_json};',
            content, flags=re.DOTALL
        )

        # Replace let GENERATED = "..."
        content = re.sub(
            r'let GENERATED = "[^"]*"',
            f'let GENERATED = "{data["generated"]}"',
            content
        )

        with open(idx_path, "w", encoding="utf-8") as f:
            f.write(content)

        refresh_status["log"].append(
            f"[{ts()}] ✅ index.html actualizado — {len(data['dsrData'])} DSRs."
        )
        refresh_status["last"] = datetime.now().isoformat()

    except Exception as e:
        refresh_status["log"].append(f"[{ts()}] ❌ Error: {str(e)}")
    finally:
        refresh_status["running"] = False


class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "port": PORT})
        elif self.path == "/status":
            self._json(refresh_status)
        elif self.path == "/data":
            try:
                data = get_data()
                self._json({"ok": True, **{k: v for k, v in data.items() if k != "log"}})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
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
    print(f"🔄 LATAM PS Dashboard — Refresh Server")
    print(f"   http://localhost:{PORT}/health — estado del servidor")
    print(f"   http://localhost:{PORT}/data   — datos frescos de org62 (cache 5 min)")
    print(f"   http://localhost:{PORT}/refresh — actualiza index.html")
    print(f"   Dashboard: {DASHBOARD_DIR}/index.html")
    print(f"   Ctrl+C para detener\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
        sys.exit(0)
