#!/usr/bin/env python3
"""
LATAM PS Dashboard — Data Refresh Script
Fetches fresh data from org62 and updates index.html in-place.

Authentication:
  - CI / Heroku: set ORG62_SFDX_URL env var (format: force://PlatformCLI::<token>@<host>)
  - Local: uses SF CLI (sf data query --target-org org62) — auto-handles token refresh

Usage:
  python3 refresh-data.py
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime

DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML    = os.path.join(DASHBOARD_DIR, "index.html")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Authentication ─────────────────────────────────────────────────────────────

def get_auth():
    """Returns (access_token, instance_url) or (None, None) to use SF CLI mode."""
    sfdx_url = os.environ.get("ORG62_SFDX_URL", "").strip()
    if not sfdx_url:
        return None, None  # local mode: use SF CLI
    # Parse:  force://PlatformCLI::<refresh_token>@<instance_host>
    m = re.match(r'force://[^:]+::([^@]+)@(.+)', sfdx_url)
    if not m:
        raise ValueError("ORG62_SFDX_URL format unrecognized. Expected: force://PlatformCLI::<token>@<host>")
    refresh_token, instance_host = m.group(1), m.group(2).rstrip('/')
    instance_url = f"https://{instance_host}"
    log(f"Refreshing OAuth token for {instance_host}...")
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
    log("Token obtained.")
    return token, result.get('instance_url', instance_url)


# ── SOQL helpers ───────────────────────────────────────────────────────────────

def _sf_bin():
    for p in [os.path.expanduser("~/.aisuite/bin/sf"), "/usr/local/bin/sf", "sf"]:
        if p == "sf" or os.path.isfile(p):
            return p

def _soql_cli(query):
    env = os.environ.copy()
    env["SF_AUTOUPDATE_DISABLE"] = "true"
    result = subprocess.run(
        [_sf_bin(), "data", "query", "--target-org", "org62", "--query", query, "--json"],
        capture_output=True, text=True, timeout=90, env=env
    )
    # Tolerate non-zero exit when stderr only contains update warnings
    stderr_clean = result.stderr.strip()
    only_warning = all('Warning:' in line or not line.strip() for line in stderr_clean.splitlines())
    if result.returncode != 0 and not only_warning:
        raise RuntimeError(f"SF CLI error: {stderr_clean[:400]}")
    return json.loads(result.stdout).get("result", {}).get("records", [])

def _soql_rest(instance_url, token, query):
    records = []
    url = f"{instance_url}/services/data/v62.0/query/?q={urllib.parse.quote(query)}"
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        records.extend(data.get("records", []))
        next_path = None if data.get("done") else data.get("nextRecordsUrl")
        url = (instance_url + next_path) if next_path else None
    return records

def soql(query, instance_url=None, token=None):
    if token:
        return _soql_rest(instance_url, token, query)
    return _soql_cli(query)


# ── Data Fetching ──────────────────────────────────────────────────────────────

def fetch(instance_url, token):
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
        "AND (Region__c = 'LACA' OR Region__c = 'LATAM') "
        "AND Request_Type_Detail__c IN ('Deal Strategy', 'RFP', 'Scoping/SOW', 'SOW / SPW Generation') "
        "AND Opportunity__r.CloseDate >= 2026-02-01 "
        "AND Opportunity__r.CloseDate <= 2028-01-31 "
        "ORDER BY Owner.Name, Opportunity__r.StageName, Status__c "
        "LIMIT 500",
        instance_url, token
    )
    log(f"{len(dsr_records)} DSRs obtenidos.")

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
        opp_list = list(opp_ids)
        log(f"Consultando estimates para {len(opp_list)} opps...")
        for i in range(0, len(opp_list), 90):
            batch   = opp_list[i:i + 90]
            ids_str = "','".join(batch)
            est_records = soql(
                f"SELECT Id, Name, ffscpq__Billing_Type__c, ffscpq__Opportunity__c, "
                f"Sub_Region__c, Estimated_ExpenseTotal_Price__c, ffscpq__Percent_Discount__c "
                f"FROM ffscpq__Estimate__c "
                f"WHERE ffscpq__Is_Primary__c = true "
                f"AND ffscpq__Opportunity__c IN ('{ids_str}') "
                f"LIMIT 100",
                instance_url, token
            )
            for e in est_records:
                oid = e.get("ffscpq__Opportunity__c")
                if oid:
                    estimate_map[oid] = {
                        "id":             e["Id"],
                        "name":           e.get("Name", ""),
                        "billingType":    e.get("ffscpq__Billing_Type__c", ""),
                        "rateCardRegion": e.get("Sub_Region__c", "LATAM"),
                        "tAndE":          e.get("Estimated_ExpenseTotal_Price__c"),
                        "discountPct":    e.get("ffscpq__Percent_Discount__c") or 0,
                    }
        log(f"{len(estimate_map)} estimates obtenidos.")

    # ── Resource Requests ──────────────────────────────────────────────────────
    latam_region_ids = (
        "a99300000000015AAA','a990M000000K1K3QAK"
        "','a990M0000008OY3QAM','a990M0000008OXyQAM"
    )
    log("Consultando Resource Requests en org62...")
    rr_records = soql(
        "SELECT Id, Name, pse__Start_Date__c, pse__End_Date__c, pse__Status__c, "
        "pse__Region__r.Name, pse__Resource__r.Name, "
        "pse__Opportunity__c, pse__Opportunity__r.Name, pse__Opportunity__r.StageName, "
        "pse__Opportunity__r.CloseDate, pse__Opportunity__r.Amount, "
        "pse__Opportunity__r.Owner.Name, pse__Opportunity__r.Account.Name "
        f"FROM pse__Resource_Request__c "
        f"WHERE pse__Region__c IN ('{latam_region_ids}') "
        "AND pse__Status__c IN ('Draft', 'Ready to Staff') "
        "AND pse__Opportunity__r.StageName NOT IN ('06 - Project Booked', 'Dead - Lost') "
        "AND pse__Opportunity__r.CloseDate >= TODAY "
        "ORDER BY pse__Opportunity__r.CloseDate, pse__Start_Date__c "
        "LIMIT 2000",
        instance_url, token
    )
    log(f"{len(rr_records)} RRs obtenidos.")

    rr_data = []
    for r in rr_records:
        opp     = r.get("pse__Opportunity__r") or {}
        region  = (r.get("pse__Region__r") or {}).get("Name", "")
        resource = (r.get("pse__Resource__r") or {}).get("Name", None)
        rr_data.append({
            "id":        r["Id"],
            "name":      r.get("Name", ""),
            "oppId":     r.get("pse__Opportunity__c", ""),
            "oppName":   opp.get("Name", ""),
            "account":   (opp.get("Account") or {}).get("Name", ""),
            "stage":     opp.get("StageName", ""),
            "closeDate": opp.get("CloseDate", ""),
            "ap":        (opp.get("Owner") or {}).get("Name", ""),
            "resource":  resource,
            "status":    r.get("pse__Status__c", ""),
            "startDate": r.get("pse__Start_Date__c", ""),
            "endDate":   r.get("pse__End_Date__c", ""),
            "sowHours":  None,
            "country":   region,
        })

    return dsr_data, estimate_map, rr_data


# ── HTML Update ────────────────────────────────────────────────────────────────

def update_html(dsr_data, estimate_map, rr_data):
    log(f"Actualizando {INDEX_HTML}...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        content = f.read()

    today    = datetime.now().strftime("%Y-%m-%d")
    dsr_json = json.dumps(dsr_data,     ensure_ascii=False, indent=2)
    est_json = json.dumps(estimate_map, ensure_ascii=False, indent=2)
    rr_json  = json.dumps(rr_data,      ensure_ascii=False, indent=2)

    content, n1 = re.subn(r'let DSR_DATA = \[.*?\];',
                          f'let DSR_DATA = {dsr_json};', content, flags=re.DOTALL)
    content, n2 = re.subn(r'let ESTIMATE_MAP = \{.*?\};',
                          f'let ESTIMATE_MAP = {est_json};', content, flags=re.DOTALL)
    content, n3 = re.subn(r'let GENERATED = "[^"]*"',
                          f'let GENERATED = "{today}"', content)
    content, n4 = re.subn(r'const RR_DATA = \[.*?\];',
                          f'const RR_DATA = {rr_json};', content, flags=re.DOTALL)

    if not (n1 and n2 and n3 and n4):
        raise RuntimeError(
            f"Pattern replacements failed: DSR_DATA={n1}, ESTIMATE_MAP={n2}, GENERATED={n3}, RR_DATA={n4}."
        )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"✅ index.html actualizado — {len(dsr_data)} DSRs, {len(estimate_map)} estimates, {len(rr_data)} RRs, {today}")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        token, instance_url = get_auth()
        dsr_data, estimate_map, rr_data = fetch(instance_url, token)
        update_html(dsr_data, estimate_map, rr_data)
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
