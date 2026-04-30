"""
connectors/upload_servicenow_data.py
======================================
Reads the Excel sample data file and creates ServiceNow records:
  - Change Requests  (change_request table)
  - Change Tasks     (change_task table)
  - CMDB CIs         (cmdb_ci table)
  - Incidents        (incident table)

Requires ServiceNow credentials in .env:
    SERVICENOW_INSTANCE=https://yourcompany.service-now.com
    SERVICENOW_USER=admin
    SERVICENOW_PASSWORD=admin

Usage:
    python connectors/upload_servicenow_data.py              # live upload
    python connectors/upload_servicenow_data.py --dry-run    # preview only
    python connectors/upload_servicenow_data.py --type cr    # only Change Requests
"""

import argparse
import os
import sys
from pathlib import Path

import openpyxl
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

SN_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
SN_USER     = os.getenv("SERVICENOW_USER", "")
SN_PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")

EXCEL_PATH  = _ROOT / "Release_Management_Platform_Sample_Data (1).xlsx"
WIDTH       = 62

# ServiceNow state mappings
CR_STATE_MAP = {
    "draft":               "-5",
    "assess (new)":        "-4",
    "authorize":           "-3",
    "scheduled":           "1",
    "scheduled (approved)":"1",
    "implement":           "2",
    "review":              "3",
    "closed":              "4",
}

CR_RISK_MAP = {
    "high":   "1",
    "medium": "2",
    "low":    "3",
}

CAB_MAP = {
    "approved":       "approved",
    "pending":        "requested",
    "not submitted":  "not_requested",
}

INC_SEVERITY_MAP = {
    "sev-1": "1",
    "sev-2": "2",
    "sev-3": "3",
    "sev-4": "4",
}

INC_STATE_MAP = {
    "new":         "1",
    "in progress": "2",
    "resolved":    "6",
    "closed":      "7",
}


def _line(char="-"):
    print(char * WIDTH)


def _auth():
    return HTTPBasicAuth(SN_USER, SN_PASSWORD)


def _headers():
    return {"Accept": "application/json", "Content-Type": "application/json"}


# ── ServiceNow REST helpers ────────────────────────────────────────────────────

def _post(table: str, body: dict, dry_run: bool) -> dict:
    """POST a record to a ServiceNow table. Returns created record or {}."""
    if dry_run:
        return {"sys_id": "DRY-RUN", "number": body.get("number", "DRY-RUN")}

    url = f"{SN_INSTANCE}/api/now/table/{table}"
    try:
        r = requests.post(url, headers=_headers(), auth=_auth(),
                          json=body, timeout=30)
    except requests.exceptions.ConnectionError:
        print(f"    [ERROR] Cannot connect to {SN_INSTANCE}")
        return {}

    if r.status_code in (200, 201):
        return r.json().get("result", {})
    else:
        print(f"    [ERROR] {r.status_code}: {r.text[:200]}")
        return {}


def _get_sys_id(table: str, field: str, value: str) -> str:
    """Find the sys_id of an existing record by field=value."""
    if not value:
        return ""
    url = f"{SN_INSTANCE}/api/now/table/{table}"
    params = {
        "sysparm_query":  f"{field}={value}",
        "sysparm_fields": "sys_id",
        "sysparm_limit":  1,
    }
    try:
        r = requests.get(url, headers=_headers(), auth=_auth(),
                         params=params, timeout=20)
        if r.status_code == 200:
            results = r.json().get("result", [])
            return results[0]["sys_id"] if results else ""
    except Exception:
        pass
    return ""


# ── Excel loader ───────────────────────────────────────────────────────────────

def load_servicenow_sheet() -> list:
    wb = openpyxl.load_workbook(EXCEL_PATH)
    ws = wb["ServiceNow Data"]
    headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if any(v is not None for v in row):
            rows.append(dict(zip(headers, row)))
    return rows


def _clean(val) -> str:
    """Convert cell value to clean string, handle None and emoji artifacts."""
    if val is None:
        return ""
    s = str(val).strip()
    # Remove common Excel encoding artifacts
    return s if s not in ("—", "–", "�", "None") else ""


# ── Record builders ────────────────────────────────────────────────────────────

def build_cr_body(row: dict) -> dict:
    state_raw = _clean(row.get("State", "")).lower()
    risk_raw  = _clean(row.get("Risk", "")).lower()
    cab_raw   = _clean(row.get("CAB Decision", "")).lower()

    start = _clean(row.get("Scheduled Start", "")).replace(" ", "T")
    end   = _clean(row.get("Scheduled End",   "")).replace(" ", "T")

    return {
        "short_description":     _clean(row.get("Short Description", "")),
        "description":           f"Project: {_clean(row.get('Project/Team', ''))}",
        "state":                 CR_STATE_MAP.get(state_raw, "-4"),
        "risk":                  CR_RISK_MAP.get(risk_raw, "2"),
        "cab_decision":          CAB_MAP.get(cab_raw, "not_requested"),
        "scheduled_start_date":  start or "",
        "scheduled_end_date":    end or "",
        "cmdb_ci":               _clean(row.get("Affected CIs", "")).split(",")[0].strip(),
        "category":              "software",
        "type":                  "normal",
    }


def build_ctask_body(row: dict, cr_sys_id: str) -> dict:
    return {
        "short_description": _clean(row.get("Short Description", "")),
        "state":             "1",  # Open
        "change_request":    cr_sys_id,
    }


def build_ci_body(row: dict) -> dict:
    return {
        "name":               _clean(row.get("Number", "")),
        "short_description":  _clean(row.get("Short Description", "")),
        "used_for":           "Production",
        "operational_status": "1",  # Operational
    }


def build_incident_body(row: dict) -> dict:
    sev_raw   = _clean(row.get("Risk", "")).lower()
    state_raw = _clean(row.get("State", "")).lower()
    return {
        "short_description": _clean(row.get("Short Description", "")),
        "severity":          INC_SEVERITY_MAP.get(sev_raw, "3"),
        "state":             INC_STATE_MAP.get(state_raw, "1"),
        "description":       f"Linked to: {_clean(row.get('Affected CIs', ''))}",
        "category":          "software",
    }


# ── Upload functions ───────────────────────────────────────────────────────────

def upload_change_requests(rows: list, dry_run: bool) -> dict:
    """Upload Change Requests. Returns {number: sys_id} for linking tasks."""
    cr_rows = [r for r in rows if (r.get("Record Type") or "").strip() == "Change Request"]
    print(f"\n  Change Requests ({len(cr_rows)} records)")
    _line()

    cr_sys_ids = {}
    for row in cr_rows:
        number = _clean(row.get("Number", ""))
        desc   = _clean(row.get("Short Description", ""))
        body   = build_cr_body(row)

        if dry_run:
            print(f"  [DRY-RUN] {number} — {desc[:55]}")
            cr_sys_ids[number] = "DRY-RUN"
        else:
            result = _post("change_request", body, dry_run=False)
            if result:
                sys_id = result.get("sys_id", "")
                created_num = result.get("number", number)
                print(f"  [CREATED] {created_num} — {desc[:50]} (sys_id={sys_id[:8]}...)")
                cr_sys_ids[number] = sys_id
            else:
                cr_sys_ids[number] = ""

    return cr_sys_ids


def upload_change_tasks(rows: list, cr_sys_ids: dict, dry_run: bool):
    """Upload Change Tasks linked to their parent CRs."""
    task_rows = [r for r in rows if (r.get("Record Type") or "").strip() == "Change Task"]
    print(f"\n  Change Tasks ({len(task_rows)} records)")
    _line()

    # Map project to CR sys_id (task descriptions mention project names)
    project_cr_map = {
        "Atlas": cr_sys_ids.get("CHG0001001", ""),
        "Delta": cr_sys_ids.get("CHG0001004", ""),
    }

    for row in task_rows:
        number  = _clean(row.get("Number", ""))
        desc    = _clean(row.get("Short Description", ""))
        project = _clean(row.get("Project/Team", ""))
        cr_id   = project_cr_map.get(project, "")
        body    = build_ctask_body(row, cr_id)

        if dry_run:
            print(f"  [DRY-RUN] {number} — {desc[:50]} (parent: {project})")
        else:
            result = _post("change_task", body, dry_run=False)
            if result:
                created_num = result.get("number", number)
                print(f"  [CREATED] {created_num} — {desc[:50]}")


def upload_cmdb_cis(rows: list, dry_run: bool):
    """Upload CMDB Configuration Items."""
    ci_rows = [r for r in rows if (r.get("Record Type") or "").strip() == "CMDB CI"]
    print(f"\n  CMDB CIs ({len(ci_rows)} records)")
    _line()

    for row in ci_rows:
        name  = _clean(row.get("Number", ""))
        desc  = _clean(row.get("Short Description", ""))
        body  = build_ci_body(row)

        if dry_run:
            print(f"  [DRY-RUN] {name} — {desc}")
        else:
            result = _post("cmdb_ci", body, dry_run=False)
            if result:
                sys_id = result.get("sys_id", "")
                print(f"  [CREATED] {name} — {desc} (sys_id={sys_id[:8]}...)")


def upload_incidents(rows: list, dry_run: bool):
    """Upload Incidents."""
    inc_rows = [r for r in rows if (r.get("Record Type") or "").strip() == "Incident"]
    print(f"\n  Incidents ({len(inc_rows)} records)")
    _line()

    for row in inc_rows:
        number = _clean(row.get("Number", ""))
        desc   = _clean(row.get("Short Description", ""))
        body   = build_incident_body(row)

        if dry_run:
            print(f"  [DRY-RUN] {number} — {desc[:55]}")
        else:
            result = _post("incident", body, dry_run=False)
            if result:
                created_num = result.get("number", number)
                print(f"  [CREATED] {created_num} — {desc[:50]}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run(upload_type: str = "all", dry_run: bool = False):
    _line("=")
    mode = "DRY-RUN (no changes will be made)" if dry_run else "LIVE UPLOAD"
    print(f"  SERVICENOW UPLOAD  [{mode}]".center(WIDTH))
    _line("=")

    if not dry_run and not all([SN_INSTANCE, SN_USER, SN_PASSWORD]):
        print("\n[ERROR] Missing ServiceNow credentials in .env")
        print("  Set SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD\n")
        sys.exit(1)

    if not dry_run:
        print(f"\n  Instance : {SN_INSTANCE}")

    print(f"  Loading Excel: {EXCEL_PATH.name}")
    rows = load_servicenow_sheet()

    by_type = {}
    for r in rows:
        t = (r.get("Record Type") or "").strip()
        by_type.setdefault(t, 0)
        by_type[t] += 1

    print(f"  Records found:")
    for t, n in by_type.items():
        print(f"    {t:20} : {n}")

    cr_sys_ids = {}

    if upload_type in ("all", "cr"):
        cr_sys_ids = upload_change_requests(rows, dry_run)

    if upload_type in ("all", "task"):
        upload_change_tasks(rows, cr_sys_ids, dry_run)

    if upload_type in ("all", "ci"):
        upload_cmdb_cis(rows, dry_run)

    if upload_type in ("all", "incident"):
        upload_incidents(rows, dry_run)

    print()
    _line("=")
    print("  UPLOAD COMPLETE".center(WIDTH))
    _line("=")
    if not dry_run:
        print(f"\n  View records at: {SN_INSTANCE}/now/nav/ui/classic/params/target/change_request_list.do\n")
    else:
        print("\n  Re-run without --dry-run to create real records.\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload Excel data to ServiceNow")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview records without creating them")
    parser.add_argument("--type", choices=["all", "cr", "task", "ci", "incident"],
                        default="all",
                        help="Which record type to upload (default: all)")
    args = parser.parse_args()
    run(upload_type=args.type, dry_run=args.dry_run)
