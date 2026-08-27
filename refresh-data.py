#!/usr/bin/env python3
"""
LATAM PS Dashboard — Data Refresh Script
Fetches fresh data from org62 via SF CLI and updates index.html in-place.
Runs locally or in CI (GitSoma Actions).

Usage:
  python3 refresh-data.py
  python3 refresh-data.py --org org62   # explicit org alias
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(DASHBOARD_DIR, "index.html")

# Locate SF CLI
def find_sf():
    for p in [
        os.path.expanduser("~/.aisuite/bin/sf"),
        "/usr/local/bin/sf",
        "/usr/bin/sf",
    ]:
        if os.path.isfile(p):
            return p
    return "sf"  # assume it's on PATH (CI installs it there)


SF_BIN = find_sf()
ORG = sys.argv[sys.argv.index("--org") + 1] if "--org" in sys.argv else "org62"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def soql(query):
    result = subprocess.run(
        [SF_BIN, "data", "query", "--target-org", ORG, "--query", query, "--json"],
        capture_output=True, text=True, timeout=90
    )
    if result.returncode != 0:
        raise RuntimeError(f"SF CLI error (rc={result.returncode}): {result.stderr[:600]}")
    data = json.loads(result.stdout)
    if data.get("status") != 0:
        raise RuntimeError(f"SOQL error: {json.dumps(data.get('result', {}))[:400]}")
    return data.get("result", {}).get("records", [])


def fetch():
    log("Consultando DSRs en org62...")
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
    log(f"{len(dsr_records)} DSRs obtenidos. Procesando...")

    dsr_data = []
    opp_ids = set()
    for r in dsr_records:
        opp = r.get("Opportunity__r") or {}
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
        opp_list = list(opp_ids)
        log(f"Consultando estimates para {len(opp_list)} opps...")
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
        log(f"{len(estimate_map)} estimates obtenidos.")

    return dsr_data, estimate_map


def update_html(dsr_data, estimate_map):
    log(f"Leyendo {INDEX_HTML}...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        content = f.read()

    today = datetime.now().strftime("%Y-%m-%d")
    dsr_json = json.dumps(dsr_data, ensure_ascii=False, indent=2)
    est_json = json.dumps(estimate_map, ensure_ascii=False, indent=2)

    # Replace let DSR_DATA = [...]
    content, n1 = re.subn(
        r'let DSR_DATA = \[.*?\];',
        f'let DSR_DATA = {dsr_json};',
        content, flags=re.DOTALL
    )
    # Replace let ESTIMATE_MAP = {...}
    content, n2 = re.subn(
        r'let ESTIMATE_MAP = \{.*?\};',
        f'let ESTIMATE_MAP = {est_json};',
        content, flags=re.DOTALL
    )
    # Replace let GENERATED = "..."
    content, n3 = re.subn(
        r'let GENERATED = "[^"]*"',
        f'let GENERATED = "{today}"',
        content
    )

    if not (n1 and n2 and n3):
        raise RuntimeError(
            f"Pattern replacements: DSR_DATA={n1}, ESTIMATE_MAP={n2}, GENERATED={n3}. "
            "Check that index.html still uses `let` declarations."
        )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(content)

    log(f"✅ index.html actualizado — {len(dsr_data)} DSRs, {len(estimate_map)} estimates, fecha={today}")


if __name__ == "__main__":
    try:
        dsr_data, estimate_map = fetch()
        update_html(dsr_data, estimate_map)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
