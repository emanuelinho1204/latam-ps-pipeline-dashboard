#!/usr/bin/env python3
"""
LATAM PS Dashboard — Heroku Proxy Server
Fetches live data from org62 and serves it to the dashboard via CORS.

Required env vars (set via Heroku config):
  ORG62_SFDX_URL  — SFDX auth URL (force://PlatformCLI::<token>@<host>)
  PORT            — set automatically by Heroku

Deploy:
  heroku create <app-name>
  heroku config:set ORG62_SFDX_URL="force://PlatformCLI::..."
  git subtree push --prefix . heroku main
  (or use Heroku GitHub integration pointing to this repo)
"""

import http.server
import json
import os
import socketserver
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime

PORT = int(os.environ.get("PORT", 3001))

_cache = {"data": None, "ts": 0, "ttl": 300}  # 5-min TTL
_lock  = threading.Lock()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Auth ───────────────────────────────────────────────────────────────────────
# Uses SF_ACCESS_TOKEN + SF_INSTANCE_URL set by refresh-data.py relay (every 30 min).
# Tokens expire in ~2h; relay keeps Heroku current well within that window.

def get_token():
    token = os.environ.get("SF_ACCESS_TOKEN", "").strip()
    instance_url = os.environ.get("SF_INSTANCE_URL", "").strip()
    if not token or not instance_url:
        raise RuntimeError("SF_ACCESS_TOKEN or SF_INSTANCE_URL not set — relay not yet run.")
    return token, instance_url


# ── SOQL ───────────────────────────────────────────────────────────────────────

def soql(instance_url, token, query):
    records = []
    url = f"{instance_url}/services/data/v62.0/query/?q={urllib.parse.quote(query)}"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        records.extend(data.get("records", []))
        nxt = None if data.get("done") else data.get("nextRecordsUrl")
        url = (instance_url + nxt) if nxt else None
    return records


# ── Data Fetch ─────────────────────────────────────────────────────────────────

def fetch_data():
    log("Authenticating...")
    token, instance_url = get_token()

    log("Fetching DSRs...")
    dsr_records = soql(instance_url, token,
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
        "LIMIT 500"
    )

    dsr_data = []
    opp_ids  = set()
    for r in dsr_records:
        opp    = r.get("Opportunity__r") or {}
        opp_id = r.get("Opportunity__c")
        if opp_id:
            opp_ids.add(opp_id)
        dsr_data.append({
            "dsrId":             r["Id"],
            "dsr":               r.get("Name", ""),
            "sssm":              (r.get("Owner") or {}).get("Name", ""),
            "status":            r.get("Status__c", ""),
            "oppName":           opp.get("Name", ""),
            "oppId":             opp_id or "",
            "amount":            opp.get("Amount") or 0,
            "closeDate":         opp.get("CloseDate", ""),
            "ap":                (opp.get("Owner") or {}).get("Name", ""),
            "requestType":       r.get("Request_Type__c", ""),
            "forecast":          opp.get("ForecastCategoryName", ""),
            "nextStep":          opp.get("NextStep", ""),
            "region":            r.get("Region__c", ""),
            "requestTypeDetail": r.get("Request_Type_Detail__c", ""),
            "account":           (opp.get("Account") or {}).get("Name", ""),
            "country":           (opp.get("Account") or {}).get("BillingCountry", ""),
            "mgrJudgement":      opp.get("Manager_Forecast_Judgement__c"),
            "contractType":      opp.get("ContractType__c"),
            "stage":             opp.get("StageName", ""),
        })

    estimate_map = {}
    if opp_ids:
        log(f"Fetching estimates for {len(opp_ids)} opps...")
        for i in range(0, len(list(opp_ids)), 90):
            batch   = list(opp_ids)[i:i + 90]
            ids_str = "','".join(batch)
            ests    = soql(instance_url, token,
                f"SELECT Id, Name, ffscpq__Billing_Type__c, ffscpq__Opportunity__c "
                f"FROM ffscpq__Estimate__c "
                f"WHERE ffscpq__Is_Primary__c = true "
                f"AND ffscpq__Opportunity__c IN ('{ids_str}') "
                f"LIMIT 100"
            )
            for e in ests:
                oid = e.get("ffscpq__Opportunity__c")
                if oid:
                    estimate_map[oid] = {
                        "id":          e["Id"],
                        "name":        e.get("Name", ""),
                        "billingType": e.get("ffscpq__Billing_Type__c", ""),
                    }

    log("Fetching BRL exchange rate...")
    brl_usd_rate = 1 / 5.383354  # fallback
    try:
        ct_records = soql(instance_url, token,
            "SELECT IsoCode, ConversionRate FROM CurrencyType WHERE IsoCode = 'BRL' LIMIT 1"
        )
        if ct_records:
            conversion_rate = ct_records[0].get("ConversionRate", 5.383354)
            brl_usd_rate = 1 / conversion_rate
    except Exception as e:
        log(f"CurrencyType query failed: {e}")

    log(f"Done — {len(dsr_data)} DSRs, {len(estimate_map)} estimates. BRL/USD: {brl_usd_rate:.5f}")
    return {"dsrData": dsr_data, "estimateMap": estimate_map,
            "brlUsdRate": brl_usd_rate,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M")}


def get_cached():
    with _lock:
        if _cache["data"] and (time.time() - _cache["ts"] < _cache["ttl"]):
            return _cache["data"]
    data = fetch_data()
    with _lock:
        _cache["data"] = data
        _cache["ts"]   = time.time()
    return data


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path == "/health":
            self._json({"ok": True})
        elif path == "/data":
            try:
                data = get_cached()
                self._json({"ok": True, **data})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, status=500)
        elif path in ("", "/", "/index.html"):
            try:
                data = get_cached()
                html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
                with open(html_path, 'r', encoding='utf-8') as f:
                    html = f.read()
                injection = (
                    '<script>'
                    f'window.__LIVE_DSR__={json.dumps(data["dsrData"], ensure_ascii=False)};'
                    f'window.__LIVE_EST__={json.dumps(data["estimateMap"], ensure_ascii=False)};'
                    f'window.__LIVE_BRL_RATE__={data["brlUsdRate"]};'
                    f'window.__LIVE_GEN__="{data["generated"]}";'
                    '</script>\n</head>'
                )
                html = html.replace('</head>', injection, 1)
                body = html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, status=500)
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    log(f"LATAM PS Proxy starting on port {PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
