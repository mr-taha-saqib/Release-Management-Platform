"""
connectors/servicenow_connector.py
====================================
Fetches Change Requests, Change Tasks, CMDB Configuration Items,
and Incidents from a ServiceNow instance.

This is the third core integration component alongside Jira and Confluence.
ServiceNow provides the IT service management layer — change approvals (CAB),
deployment scheduling, CMDB infrastructure context, and incident tracking.

Credentials come from the .env file in the project root:
    SERVICENOW_INSTANCE=https://yourcompany.service-now.com
    SERVICENOW_USER=your_username
    SERVICENOW_PASSWORD=your_password

Main function:
    fetch_servicenow_data(project_keys)  ->  dict
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ── Credentials from .env ──────────────────────────────────────────────────────
SN_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
SN_USER     = os.getenv("SERVICENOW_USER", "")
SN_PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")

# ServiceNow table names (REST Table API)
_TABLE_CR    = "change_request"
_TABLE_CTASK = "change_task"
_TABLE_CI    = "cmdb_ci"
_TABLE_INC   = "incident"


def _is_configured() -> bool:
    """Return True if all required ServiceNow credentials are set."""
    return bool(SN_INSTANCE and SN_USER and SN_PASSWORD)


def _get(table: str, params: dict = None) -> list:
    """
    Make one authenticated GET request to the ServiceNow Table REST API.

    Endpoint: GET /api/now/table/<table>?<params>

    Parameters
    ----------
    table  : str   ServiceNow table name (e.g. "change_request").
    params : dict  Query parameters (sysparm_query, sysparm_fields, etc.).

    Returns
    -------
    list  The 'result' list from the JSON response. Empty list on error.
    """
    url  = f"{SN_INSTANCE}/api/now/table/{table}"
    auth = HTTPBasicAuth(SN_USER, SN_PASSWORD)
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}

    default_params = {
        "sysparm_limit":       100,
        "sysparm_display_value": "true",   # return display values not sys_ids
    }
    if params:
        default_params.update(params)

    try:
        resp = requests.get(url, auth=auth, headers=hdrs,
                            params=default_params, timeout=20)
    except requests.exceptions.ConnectionError:
        print(f"\n[ServiceNow] Cannot connect to: {SN_INSTANCE}")
        print("  Check SERVICENOW_INSTANCE in .env\n")
        return []

    if resp.status_code == 401:
        print("\n[ServiceNow] 401 Unauthorized — check SERVICENOW_USER and SERVICENOW_PASSWORD in .env")
        return []

    if resp.status_code == 403:
        print(f"\n[ServiceNow] 403 Forbidden for table '{table}'")
        print("  Your account may not have read access to this table.")
        return []

    if resp.status_code >= 400:
        print(f"\n[ServiceNow] HTTP {resp.status_code} for table '{table}': {resp.text[:200]}")
        return []

    return resp.json().get("result", [])


# ── Change Request fetcher ─────────────────────────────────────────────────────

def _fetch_change_requests(project_keys: list) -> list:
    """
    Fetch Change Requests (CRs) linked to the given project keys.

    Searches for CRs whose short_description contains any project key.
    In a real integration you would filter by a custom 'project' field or
    assignment group.

    Returns
    -------
    list of dict  Structured CR records.
    """
    if not project_keys:
        return []

    # Build OR query: short_description contains ATLAS OR BEACON OR ...
    query_parts = [f"short_descriptionLIKE{key}" for key in project_keys]
    query       = "^OR".join(query_parts)

    fields = ",".join([
        "number", "short_description", "state", "risk",
        "cab_decision", "scheduled_start_date", "scheduled_end_date",
        "cmdb_ci", "assignment_group", "assigned_to",
        "description", "sys_created_on", "sys_updated_on",
    ])

    raw = _get(_TABLE_CR, {
        "sysparm_query":  query,
        "sysparm_fields": fields,
    })

    results = []
    for cr in raw:
        results.append({
            "number":          cr.get("number", ""),
            "short_description": cr.get("short_description", ""),
            "state":           cr.get("state", ""),
            "risk":            cr.get("risk", ""),
            "cab_decision":    cr.get("cab_decision", ""),
            "scheduled_start": (cr.get("scheduled_start_date") or "")[:16],
            "scheduled_end":   (cr.get("scheduled_end_date")   or "")[:16],
            "ci":              _extract_display(cr.get("cmdb_ci")),
            "assignment_group": _extract_display(cr.get("assignment_group")),
            "assigned_to":     _extract_display(cr.get("assigned_to")),
            "description":     (cr.get("description") or "")[:500],
            "created":         (cr.get("sys_created_on") or "")[:10],
            "updated":         (cr.get("sys_updated_on") or "")[:10],
        })
    return results


# ── Change Task fetcher ────────────────────────────────────────────────────────

def _fetch_change_tasks(cr_numbers: list) -> list:
    """
    Fetch Change Tasks (CTASKs) that belong to the given CR numbers.

    Change Tasks are the step-by-step deployment checklist items
    under each Change Request.

    Returns
    -------
    list of dict  Structured change task records with parent CR number.
    """
    if not cr_numbers:
        return []

    query_parts = [f"change_requestNAMESTARTSWITH{n}" for n in cr_numbers]
    query       = "^OR".join(query_parts)

    fields = ",".join([
        "number", "short_description", "state",
        "assigned_to", "order", "change_request",
    ])

    raw = _get(_TABLE_CTASK, {
        "sysparm_query":  query,
        "sysparm_fields": fields,
    })

    results = []
    for ct in raw:
        results.append({
            "number":       ct.get("number", ""),
            "description":  ct.get("short_description", ""),
            "state":        ct.get("state", ""),
            "assigned_to":  _extract_display(ct.get("assigned_to")),
            "order":        ct.get("order", ""),
            "parent_cr":    _extract_display(ct.get("change_request")),
        })
    return sorted(results, key=lambda x: str(x.get("order", "0")))


# ── CMDB CI fetcher ────────────────────────────────────────────────────────────

def _fetch_cmdb_cis(ci_names: list) -> list:
    """
    Fetch CMDB Configuration Items by name.

    CIs represent the infrastructure components (databases, servers,
    applications) that will be modified by the release.

    Returns
    -------
    list of dict  Structured CI records with environment and class.
    """
    if not ci_names:
        return []

    query_parts = [f"nameLIKE{name}" for name in ci_names]
    query       = "^OR".join(query_parts)

    fields = ",".join([
        "name", "sys_class_name", "environment",
        "used_for", "operational_status", "sys_updated_on",
    ])

    raw = _get(_TABLE_CI, {
        "sysparm_query":  query,
        "sysparm_fields": fields,
    })

    results = []
    for ci in raw:
        results.append({
            "name":        ci.get("name", ""),
            "class":       ci.get("sys_class_name", ""),
            "environment": ci.get("environment", ""),
            "status":      ci.get("operational_status", ""),
            "updated":     (ci.get("sys_updated_on") or "")[:10],
        })
    return results


# ── Incident fetcher ───────────────────────────────────────────────────────────

def _fetch_incidents(cr_numbers: list) -> list:
    """
    Fetch Incidents linked to the given Change Request numbers.

    During deployment, incidents may be raised and linked to the active CR.
    The processor uses these to flag deployment health issues.

    Returns
    -------
    list of dict  Structured incident records.
    """
    if not cr_numbers:
        return []

    query_parts = [f"caused_byNAMESTARTSWITH{n}" for n in cr_numbers]
    query       = "^OR".join(query_parts)

    fields = ",".join([
        "number", "short_description", "state",
        "severity", "caused_by", "sys_created_on",
    ])

    raw = _get(_TABLE_INC, {
        "sysparm_query":  query,
        "sysparm_fields": fields,
    })

    results = []
    for inc in raw:
        results.append({
            "number":      inc.get("number", ""),
            "description": inc.get("short_description", ""),
            "state":       inc.get("state", ""),
            "severity":    inc.get("severity", ""),
            "caused_by":   _extract_display(inc.get("caused_by")),
            "created":     (inc.get("sys_created_on") or "")[:10],
        })
    return results


# ── Helper ─────────────────────────────────────────────────────────────────────

def _extract_display(field) -> str:
    """
    ServiceNow reference fields return either a string or a dict with
    'display_value'. Extract the display value in either case.
    """
    if isinstance(field, dict):
        return field.get("display_value", "")
    return field or ""


# ── Main public function ───────────────────────────────────────────────────────

def fetch_servicenow_data(project_keys: list, verbose: bool = True) -> dict:
    """
    Fetch all ServiceNow data relevant to the given project keys.

    Fetches in order:
      1. Change Requests matching project keys
      2. Change Tasks under those CRs
      3. CMDB CIs referenced by those CRs
      4. Incidents linked to those CRs

    Parameters
    ----------
    project_keys : list   List of project keys (e.g. ["ATLAS", "BEACON"]).
    verbose      : bool   Print progress messages.

    Returns
    -------
    dict
        {
            "fetched_at":       "2026-05-01",
            "change_requests":  [...],
            "change_tasks":     [...],
            "cmdb_cis":         [...],
            "incidents":        [...],
        }
        Returns empty dict if ServiceNow is not configured.
    """
    if not _is_configured():
        if verbose:
            print("  [ServiceNow] Not configured — skipping.")
            print("  Set SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD in .env")
        return {}

    if verbose:
        print(f"  Instance   : {SN_INSTANCE}")
        print(f"  Projects   : {', '.join(project_keys)}")

    # ── 1. Change Requests ─────────────────────────────────────────────────────
    if verbose:
        print("  [1/4] Fetching Change Requests ...")
    crs      = _fetch_change_requests(project_keys)
    cr_nums  = [cr["number"] for cr in crs]
    if verbose:
        print(f"        Found {len(crs)} Change Requests")

    # ── 2. Change Tasks ────────────────────────────────────────────────────────
    if verbose:
        print("  [2/4] Fetching Change Tasks ...")
    tasks = _fetch_change_tasks(cr_nums)
    if verbose:
        print(f"        Found {len(tasks)} Change Tasks")

    # ── 3. CMDB CIs ────────────────────────────────────────────────────────────
    if verbose:
        print("  [3/4] Fetching CMDB Configuration Items ...")
    # Known CIs from the Release Management Platform data guide
    known_cis = [
        "payment-db-prod", "api-gateway-prod",
        "customer-portal-prod", "reporting-server-prod", "k8s-cluster-prod",
    ]
    cis = _fetch_cmdb_cis(known_cis)
    if verbose:
        print(f"        Found {len(cis)} CMDB CIs")

    # ── 4. Incidents ───────────────────────────────────────────────────────────
    if verbose:
        print("  [4/4] Fetching linked Incidents ...")
    incidents = _fetch_incidents(cr_nums)
    if verbose:
        print(f"        Found {len(incidents)} Incidents")

    return {
        "fetched_at":      datetime.today().strftime("%Y-%m-%d"),
        "change_requests": crs,
        "change_tasks":    tasks,
        "cmdb_cis":        cis,
        "incidents":       incidents,
    }


# ── Run standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    keys = sys.argv[1:] or ["ATLAS", "BEACON", "CIPHER", "DELTA"]
    print(f"\nFetching ServiceNow data for projects: {keys}")
    data = fetch_servicenow_data(keys, verbose=True)
    if data:
        print(f"\nChange Requests : {len(data['change_requests'])}")
        print(f"Change Tasks    : {len(data['change_tasks'])}")
        print(f"CMDB CIs        : {len(data['cmdb_cis'])}")
        print(f"Incidents       : {len(data['incidents'])}")
    else:
        print("\nNo data fetched. Check ServiceNow credentials in .env")
