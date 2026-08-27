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
import re
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

def get_token():
    sfdx_url = os.environ.get("ORG62_SFDX_URL", "").strip()
    if not sfdx_url:
        raise RuntimeError("ORG62_SFDX_URL env var not set.")
    m = re.match(r'force://[^:]+::([^@]+)@(.+)', sfdx_url)
    if not m:
        raise ValueError("ORG62_SFDX_URL format unrecognized.")
    refresh_token = m.group(1)
    instance_url  = f"https://{m.group(2).rstrip('/')}"
    data = urllib.parse.urlencode({
        'grant_type':    'refresh_token',
        'client_id':     'PlatformCLI',
        'refresh_token': refresh_token,
    }).encode()
    req = urllib.request.Request(
        f"{instance_url}/services/oauth2/token", data=data, method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    token = result.get('access_token')
    if not token:
        raise RuntimeError(f"Token refresh failed: {result}")
    return token, result.get('instance_url', instance_url)


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
        "LIMIT 250"
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

    log(f"Done — {len(dsr_data)} DSRs, {len(estimate_map)} estimates.")
    return {"dsrData": dsr_data, "estimateMap": estimate_map,
            "generated": datetime.now().strftime("%Y-%m-%d")}


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
        if self.path == "/health":
            self._json({"ok": True})
        elif self.path in ("/data", "/data/"):
            try:
                data = get_cached()
                self._json({"ok": True, **data})
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


if __name__ == "__main__":
    log(f"LATAM PS Proxy starting on port {PORT}")
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
