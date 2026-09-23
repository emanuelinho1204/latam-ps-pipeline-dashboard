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
from datetime import datetime, timedelta

DASHBOARD_DIR  = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML     = os.path.join(DASHBOARD_DIR, "index.html")
SHEET_ID       = "1NfRsZqwk-CoRq2hEqAl43MEnmc6H44mnBSOgjWMU4a0"
SHEET_TAB      = "KPI History"
MCP_URL        = "http://127.0.0.1:29051/mcp/servers/google-workspace"
MCP_TOKEN      = "fe704f82-69cc-4533-afad-f57381cdbc51"


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
        "Opportunity__r.StageName, Opportunity__c, CreatedDate, "
        "Opportunity__r.LastStageChangeDate "
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
            "createdDate":       (r.get("CreatedDate") or "")[:10],
            "lastStageChange":   (opp.get("LastStageChangeDate") or "")[:10],
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


# ── Change Detection ───────────────────────────────────────────────────────────

def extract_existing_dsr_data():
    """Read current DSR_DATA from index.html before overwriting."""
    try:
        with open(INDEX_HTML, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r'let DSR_DATA = (\[.*?\]);', content, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    return []

def detect_changes(old_dsrs, new_dsrs):
    """Compare DSR snapshots, return list of field-level changes."""
    old_map = {d["dsrId"]: d for d in old_dsrs}
    changes = []
    track = [("closeDate", "CloseDate"), ("stage", "Stage"), ("status", "Status"), ("amount", "Amount")]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for d in new_dsrs:
        old = old_map.get(d["dsrId"])
        if not old:
            continue
        for field, label in track:
            ov = str(old.get(field, "") or "").strip()
            nv = str(d.get(field, "") or "").strip()
            if ov != nv and ov and nv:
                changes.append({
                    "fecha":   now,
                    "sssm":    d["sssm"],
                    "dsr":     d["dsr"],
                    "oppId":   d["oppId"],
                    "oppName": d["oppName"],
                    "campo":   label,
                    "antes":   ov,
                    "ahora":   nv,
                })
    return changes


# ── HTML Update ────────────────────────────────────────────────────────────────

def update_html(dsr_data, estimate_map, rr_data, recent_changes=None):
    log(f"Actualizando {INDEX_HTML}...")
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        content = f.read()

    today      = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    dsr_json   = json.dumps(dsr_data,            ensure_ascii=False, indent=2)
    est_json   = json.dumps(estimate_map,         ensure_ascii=False, indent=2)
    rr_json    = json.dumps(rr_data,              ensure_ascii=False, indent=2)
    chg_json   = json.dumps(recent_changes or [], ensure_ascii=False, indent=2)

    content, n1 = re.subn(r'let DSR_DATA = \[.*?\];',
                          f'let DSR_DATA = {dsr_json};', content, flags=re.DOTALL)
    content, n2 = re.subn(r'let ESTIMATE_MAP = \{.*?\};',
                          f'let ESTIMATE_MAP = {est_json};', content, flags=re.DOTALL)
    content, n3 = re.subn(r'let GENERATED = "[^"]*"',
                          f'let GENERATED = "{today}"', content)
    content, n4 = re.subn(r'const RR_DATA = \[.*?\];',
                          f'const RR_DATA = {rr_json};', content, flags=re.DOTALL)
    content, n5 = re.subn(r'const RECENT_CHANGES = \[.*?\];',
                          f'const RECENT_CHANGES = {chg_json};', content, flags=re.DOTALL)

    if not (n1 and n2 and n3 and n4):
        raise RuntimeError(
            f"Pattern replacements failed: DSR_DATA={n1}, ESTIMATE_MAP={n2}, GENERATED={n3}, RR_DATA={n4}."
        )

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(content)
    chg_count = len(recent_changes) if recent_changes else 0
    log(f"✅ index.html actualizado — {len(dsr_data)} DSRs, {len(estimate_map)} estimates, {len(rr_data)} RRs, {today}, {chg_count} cambios")


# ── Google Sheet KPI Logging ───────────────────────────────────────────────────

def _mcp_call(tool_name, arguments):
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments}
    }).encode()
    req = urllib.request.Request(
        MCP_URL, data=payload,
        headers={"Authorization": f"Bearer {MCP_TOKEN}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def _sheet_next_row(tab):
    result = _mcp_call("read_sheet_values", {
        "spreadsheet_id": SHEET_ID,
        "range_name": f"'{tab}'!A:A",
    })
    content = result.get("result", {}).get("content", [])
    raw = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
    m = re.search(r"read (\d+) rows", raw)
    return int(m.group(1)) + 1 if m else 2


def write_to_sheet(dsr_data, estimate_map, rr_data, recent_changes=None):
    try:
        today = datetime.now().date()
        today_str = today.isoformat()
        cutoff_30d = today + timedelta(days=30)
        zombie_cutoff = (today - timedelta(days=7)).isoformat()

        status_counts = {}
        for d in dsr_data:
            s = d.get("status", "")
            status_counts[s] = status_counts.get(s, 0) + 1

        pipeline_m = sum((d.get("amount") or 0) for d in dsr_data) / 1_000_000

        overdue_rr = sum(
            1 for r in rr_data
            if r.get("startDate") and r["startDate"] < today_str
        )
        close_30d_rr = sum(
            1 for r in rr_data
            if r.get("closeDate") and today_str <= r["closeDate"] <= cutoff_30d.isoformat()
        )

        def days_since(date_str):
            try:
                return (today - datetime.strptime(date_str[:10], "%Y-%m-%d").date()).days
            except Exception:
                return None

        working_dsrs = [d for d in dsr_data if d.get("status") == "Working"]
        working_days = [days_since(d["lastStageChange"]) for d in working_dsrs if d.get("lastStageChange")]
        avg_working = round(sum(working_days) / len(working_days)) if working_days else 0

        all_days = [days_since(d["lastStageChange"]) for d in dsr_data if d.get("lastStageChange")]
        stale_30d = sum(1 for x in all_days if x is not None and x > 30)
        avg_no_change = round(sum(x for x in all_days if x is not None) / len(all_days)) if all_days else 0

        fecha = datetime.now().strftime("%Y-%m-%d %H:%M")

        # ── KPI History ──────────────────────────────────────────────────────────
        row = [
            fecha,
            str(len(dsr_data)),
            str(status_counts.get("Working", 0)),
            str(status_counts.get("On Hold", 0)),
            str(status_counts.get("In Queue", 0)),
            str(status_counts.get("Waiting", 0)),
            str(status_counts.get("Vencida", 0)),
            str(len(estimate_map)),
            str(len(rr_data)),
            str(overdue_rr),
            str(close_30d_rr),
            f"{pipeline_m:.2f}",
            str(avg_working),
            str(stale_30d),
            str(avg_no_change),
        ]
        next_row = _sheet_next_row(SHEET_TAB)
        _mcp_call("modify_sheet_values", {
            "spreadsheet_id": SHEET_ID,
            "range_name": f"'{SHEET_TAB}'!A{next_row}:O{next_row}",
            "values": [row],
        })
        log(f"📊 KPI History row {next_row} escrito: {fecha}")

        # ── SSSM History ─────────────────────────────────────────────────────────
        sssm_map = {}
        for d in dsr_data:
            name = d.get("sssm") or "Unknown"
            if name not in sssm_map:
                sssm_map[name] = {
                    "total": 0, "working": 0, "onhold": 0, "inqueue": 0,
                    "waiting": 0, "vencida": 0, "arr": 0.0, "zombies": 0,
                    "working_days": [], "all_days": [],
                }
            m = sssm_map[name]
            m["total"] += 1
            st = d.get("status", "")
            if st == "Working":   m["working"] += 1
            elif st == "On Hold": m["onhold"] += 1
            elif st == "In Queue":m["inqueue"] += 1
            elif st == "Waiting": m["waiting"] += 1
            elif st == "Vencida": m["vencida"] += 1
            m["arr"] += (d.get("amount") or 0)
            cd = d.get("closeDate", "")
            if cd and cd < zombie_cutoff:
                m["zombies"] += 1
            ds = days_since(d.get("lastStageChange", ""))
            if ds is not None:
                m["all_days"].append(ds)
                if st == "Working":
                    m["working_days"].append(ds)

        sssm_rows = []
        for name, m in sorted(sssm_map.items()):
            avg_w = round(sum(m["working_days"]) / len(m["working_days"])) if m["working_days"] else 0
            s30 = sum(1 for x in m["all_days"] if x > 30)
            avg_nc = round(sum(m["all_days"]) / len(m["all_days"])) if m["all_days"] else 0
            sssm_rows.append([
                fecha, name,
                str(m["total"]), str(m["working"]), str(m["onhold"]),
                str(m["inqueue"]), str(m["waiting"]), str(m["vencida"]),
                f"{m['arr']/1_000_000:.2f}",
                str(m["zombies"]), str(avg_w), str(s30), str(avg_nc),
            ])

        if sssm_rows:
            nr = _sheet_next_row("SSSM History")
            _mcp_call("modify_sheet_values", {
                "spreadsheet_id": SHEET_ID,
                "range_name": f"'SSSM History'!A{nr}:M{nr + len(sssm_rows) - 1}",
                "values": sssm_rows,
            })
            log(f"📊 SSSM History: {len(sssm_rows)} filas escritas desde row {nr}")

        # ── Changes ───────────────────────────────────────────────────────────────
        if recent_changes:
            change_rows = [
                [
                    c.get("fecha", fecha),
                    c.get("sssm", ""),
                    c.get("dsr", ""),
                    c.get("oppName", c.get("oppId", "")),
                    c.get("campo", ""),
                    c.get("antes", ""),
                    c.get("ahora", ""),
                ]
                for c in recent_changes
            ]
            nr = _sheet_next_row("Changes")
            _mcp_call("modify_sheet_values", {
                "spreadsheet_id": SHEET_ID,
                "range_name": f"'Changes'!A{nr}:G{nr + len(change_rows) - 1}",
                "values": change_rows,
            })
            log(f"📊 Changes: {len(change_rows)} filas escritas desde row {nr}")
        else:
            log("📊 Changes: sin cambios detectados, no se escribió.")

    except Exception as e:
        log(f"⚠️  Google Sheet write skipped: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def git_push():
    today = datetime.now().strftime("%Y-%m-%d")
    cmds = [
        ["git", "-C", DASHBOARD_DIR, "add", "index.html"],
        ["git", "-C", DASHBOARD_DIR, "commit", "-m", f"Refresh data {today}"],
        ["git", "-C", DASHBOARD_DIR, "push", "origin", "master"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # commit fails with code 1 when there's nothing to commit — skip silently
            if "nothing to commit" in result.stdout + result.stderr:
                log("Git: sin cambios, no se hizo commit.")
                return
            raise RuntimeError(f"Git error ({' '.join(cmd[2:])}): {result.stderr[:300]}")
    log("🚀 Push a origin master OK.")


HEROKU_APP = "aqueous-peak-73896"
SF_BIN_PATH = None  # resolved lazily


def _sf_bin_path():
    global SF_BIN_PATH
    if SF_BIN_PATH:
        return SF_BIN_PATH
    for p in [os.path.expanduser("~/.aisuite/bin/sf"), "/usr/local/bin/sf", "sf"]:
        if p == "sf" or os.path.isfile(p):
            SF_BIN_PATH = p
            return p
    return "sf"


def relay_token_to_heroku():
    """Push current SF access token to Heroku so heroku-server.py can use it directly."""
    try:
        import tempfile, re as _re
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        env = {**os.environ, "SF_TEMP_SHOW_SECRETS": "true"}
        with open(tmp.name, "wb") as out:
            subprocess.run(
                ["bash", "-c",
                 f'echo y | {_sf_bin_path()} org display --target-org org62 --verbose --json'],
                stdout=out, stderr=subprocess.DEVNULL, timeout=30, env=env
            )
        with open(tmp.name, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
        os.unlink(tmp.name)
        clean = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\r|\x0b|\x0c|[\x00-\x08\x0e-\x1f]', '', raw)
        m = _re.search(r'(\{.*\})', clean, _re.DOTALL)
        if not m:
            log("relay_token: JSON not found in sf org display output — skipping.")
            return
        org = json.loads(m.group(1)).get("result", {})
        access_token = org.get("accessToken", "")
        instance_url = org.get("instanceUrl", "")
        if not access_token or not instance_url:
            log("relay_token: accessToken not found — skipping.")
            return
        heroku = "/opt/homebrew/bin/heroku"
        subprocess.run(
            [heroku, "config:set",
             f"SF_ACCESS_TOKEN={access_token}",
             f"SF_INSTANCE_URL={instance_url}",
             "--app", HEROKU_APP],
            capture_output=True, text=True, timeout=30
        )
        log(f"✅ Token relay → Heroku ({HEROKU_APP}) OK.")
    except Exception as e:
        log(f"relay_token: failed (non-critical): {e}")


if __name__ == "__main__":
    try:
        token, instance_url = get_auth()
        old_dsrs = extract_existing_dsr_data()
        dsr_data, estimate_map, rr_data = fetch(instance_url, token)
        recent_changes = detect_changes(old_dsrs, dsr_data)
        if recent_changes:
            log(f"⚡ {len(recent_changes)} cambios detectados: " +
                ", ".join(f"{c['campo']}:{c['dsr']}" for c in recent_changes[:6]))
        update_html(dsr_data, estimate_map, rr_data, recent_changes)
        write_to_sheet(dsr_data, estimate_map, rr_data, recent_changes)
        git_push()
        relay_token_to_heroku()
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)
