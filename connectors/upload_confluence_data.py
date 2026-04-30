"""
connectors/upload_confluence_data.py
=====================================
Reads the Excel sample data file and creates Confluence pages
for each project space (ATLAS, BEACON, CIPHER, DELTA).

Pages created per project:
  1. Release Plan
  2. Deployment Runbook
  3. Test Evidence
  4. Rollback Procedure
  5. Release Notes

Usage:
    python connectors/upload_confluence_data.py              # live upload
    python connectors/upload_confluence_data.py --dry-run    # preview only
    python connectors/upload_confluence_data.py --project ATLAS
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

DOMAIN = os.getenv("ATLASSIAN_DOMAIN", "").rstrip("/")
EMAIL  = os.getenv("ATLASSIAN_EMAIL", "")
TOKEN  = os.getenv("ATLASSIAN_API_TOKEN", "")

EXCEL_PATH = _ROOT / "Release_Management_Platform_Sample_Data (1).xlsx"

WIDTH = 62


def _line(char="-"):
    print(char * WIDTH)


def _auth():
    return HTTPBasicAuth(EMAIL, TOKEN)


def _headers():
    return {"Accept": "application/json", "Content-Type": "application/json"}


# ── Confluence API helpers ─────────────────────────────────────────────────────

def get_existing_pages(space_key: str) -> dict:
    """Return {title: page_id} for all pages in the space."""
    url = f"{DOMAIN}/wiki/rest/api/content"
    params = {"spaceKey": space_key, "type": "page", "limit": 100,
              "expand": "version"}
    r = requests.get(url, headers=_headers(), auth=_auth(), params=params, timeout=20)
    if r.status_code != 200:
        return {}
    results = r.json().get("results", [])
    return {p["title"]: p["id"] for p in results}


def create_page(space_key: str, title: str, html: str, dry_run: bool) -> str:
    """Create a new Confluence page. Returns page ID or 'DRY-RUN'."""
    if dry_run:
        print(f"    [DRY-RUN] Would create: '{title}'")
        return "DRY-RUN"
    url = f"{DOMAIN}/wiki/rest/api/content"
    body = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {"storage": {"value": html, "representation": "storage"}},
    }
    r = requests.post(url, headers=_headers(), auth=_auth(), json=body, timeout=20)
    if r.status_code in (200, 201):
        page_id = r.json()["id"]
        print(f"    [CREATED] '{title}' (id={page_id})")
        return page_id
    else:
        print(f"    [ERROR]   '{title}' — {r.status_code}: {r.text[:120]}")
        return ""


def update_page(page_id: str, space_key: str, title: str, html: str, dry_run: bool):
    """Update an existing Confluence page."""
    if dry_run:
        print(f"    [DRY-RUN] Would update: '{title}' (id={page_id})")
        return
    # Get current version
    url = f"{DOMAIN}/wiki/rest/api/content/{page_id}?expand=version"
    r = requests.get(url, headers=_headers(), auth=_auth(), timeout=20)
    if r.status_code != 200:
        print(f"    [ERROR]   Cannot fetch version for '{title}'")
        return
    version = r.json()["version"]["number"] + 1
    body = {
        "type": "page",
        "title": title,
        "version": {"number": version},
        "space": {"key": space_key},
        "body": {"storage": {"value": html, "representation": "storage"}},
    }
    r2 = requests.put(f"{DOMAIN}/wiki/rest/api/content/{page_id}",
                      headers=_headers(), auth=_auth(), json=body, timeout=20)
    if r2.status_code == 200:
        print(f"    [UPDATED] '{title}' (id={page_id})")
    else:
        print(f"    [ERROR]   '{title}' — {r2.status_code}: {r2.text[:120]}")


def upsert_page(space_key: str, title: str, html: str,
                existing: dict, dry_run: bool):
    """Create page if it doesn't exist, update if it does."""
    if title in existing:
        update_page(existing[title], space_key, title, html, dry_run)
    else:
        create_page(space_key, title, html, dry_run)


# ── Excel readers ──────────────────────────────────────────────────────────────

def load_excel() -> dict:
    """Load all relevant sheets from the Excel file."""
    wb = openpyxl.load_workbook(EXCEL_PATH)

    def sheet_rows(name):
        ws = wb[name]
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if any(v is not None for v in row):
                rows.append(dict(zip(headers, row)))
        return rows

    return {
        "overview":   sheet_rows("Project Overview"),
        "tickets":    sheet_rows("Jira Tickets"),
        "conf_pages": sheet_rows("Confluence Pages"),
        "servicenow": sheet_rows("ServiceNow Data"),
        "history":    sheet_rows("Historical Data (RAG)"),
        "deps":       sheet_rows("Dependency Map"),
    }


# ── HTML page builders ─────────────────────────────────────────────────────────

def _status_color(status: str) -> str:
    s = (status or "").lower()
    if "done" in s or "complete" in s or "green" in s:
        return "#00875A"
    if "progress" in s or "amber" in s or "draft" in s or "incomplete" in s:
        return "#FF8B00"
    if "red" in s or "missing" in s or "blocked" in s:
        return "#DE350B"
    return "#6B778C"


def _badge(text: str, color: str) -> str:
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:3px;font-size:12px;">{text}</span>')


def build_release_plan(proj: dict, tickets: list, deps: list, sn: list) -> str:
    key   = proj["Jira Key"]
    name  = proj["Project"]
    desc  = proj["Description"]
    team  = proj["Team"]
    size  = proj["Team Size"]
    ready = proj["Expected Readiness"] or ""
    ver   = proj["Fix Version"] or ""
    db    = proj["Has DB Migration"] or "No"
    dep   = proj["Has Dependencies"] or "No"

    color = _status_color(ready)
    badge = _badge(ready, color)

    proj_tickets = [t for t in tickets if t["Project"] == key]
    done  = sum(1 for t in proj_tickets if (t["Status"] or "").lower() == "done")
    total = len(proj_tickets)

    proj_deps = [d for d in deps if d["Source Project"] == key or d["Target Project"] == key]
    proj_sn   = [s for s in sn if key in str(s.get("Project/Team", ""))]

    dep_rows = "".join(
        f"<tr><td>{d['Source Ticket']}</td><td>{d['Link Type']}</td>"
        f"<td>{d['Target Ticket']}</td><td>{d['Dependency Reason']}</td>"
        f"<td>{_badge(d['Conflict Risk'] or 'Low', _status_color(d['Conflict Risk'] or ''))}</td></tr>"
        for d in proj_deps
    ) or "<tr><td colspan='5'>No dependencies</td></tr>"

    sn_rows = "".join(
        f"<tr><td>{s['Record Type']}</td><td><strong>{s['Number']}</strong></td>"
        f"<td>{s['Short Description']}</td>"
        f"<td>{_badge(s['State'] or '', _status_color(s['State'] or ''))}</td>"
        f"<td>{s['CAB Decision'] or '—'}</td></tr>"
        for s in proj_sn
    ) or "<tr><td colspan='5'>No ServiceNow records</td></tr>"

    return f"""
<h1>{name} — Release Plan</h1>
<p><strong>Version:</strong> {ver} &nbsp;|&nbsp;
   <strong>Team:</strong> {team} ({size} members) &nbsp;|&nbsp;
   <strong>Description:</strong> {desc}</p>
<p><strong>Readiness:</strong> {badge}</p>
<p><strong>DB Migration:</strong> {db} &nbsp;|&nbsp;
   <strong>Has Dependencies:</strong> {dep}</p>

<h2>Progress</h2>
<p>{done} of {total} tickets done ({int(done/total*100) if total else 0}% complete)</p>

<h2>Ticket Summary</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Ticket</th><th>Type</th><th>Summary</th><th>Status</th><th>Assignee</th>
  </tr>
  {"".join(
    f"<tr><td>{t['Ticket Key']}</td><td>{t['Type']}</td><td>{t['Summary']}</td>"
    f"<td>{_badge(t['Status'] or '', _status_color(t['Status'] or ''))}</td>"
    f"<td>{t['Assignee'] or 'Unassigned'}</td></tr>"
    for t in proj_tickets
  )}
</table>

<h2>Dependencies</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Source</th><th>Link</th><th>Target</th><th>Reason</th><th>Risk</th>
  </tr>
  {dep_rows}
</table>

<h2>ServiceNow / CAB</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Type</th><th>Number</th><th>Description</th><th>State</th><th>CAB Decision</th>
  </tr>
  {sn_rows}
</table>
"""


def build_deployment_runbook(proj: dict, tickets: list) -> str:
    key  = proj["Jira Key"]
    name = proj["Project"]
    ver  = proj["Fix Version"] or ""
    db   = proj["Has DB Migration"] or "No"

    deploy_tickets = [t for t in tickets if t["Project"] == key
                      and t["Type"] in ("Task", "Story")]

    steps = "".join(
        f"<li><strong>{t['Ticket Key']}</strong> — {t['Summary']} "
        f"{_badge(t['Status'] or '', _status_color(t['Status'] or ''))}</li>"
        for t in deploy_tickets
    ) or "<li>No deployment tasks found</li>"

    return f"""
<h1>{name} — Deployment Runbook</h1>
<p><strong>Version:</strong> {ver} &nbsp;|&nbsp; <strong>DB Migration:</strong> {db}</p>

<h2>Prerequisites</h2>
<ul>
  <li>All tickets in Done status before deployment</li>
  <li>CAB approval obtained from ServiceNow</li>
  <li>Rollback procedure reviewed and tested</li>
  {"<li><strong>DB migration script ready and tested on staging</strong></li>" if "Yes" in str(db) else ""}
</ul>

<h2>Deployment Steps</h2>
<ol>
  {steps}
</ol>

<h2>Post-Deployment Verification</h2>
<ul>
  <li>Smoke test all critical user journeys</li>
  <li>Check application logs for errors</li>
  <li>Verify monitoring dashboards are GREEN</li>
  <li>Confirm ServiceNow CR is closed</li>
</ul>

<h2>Rollback Trigger Conditions</h2>
<ul>
  <li>Critical error rate exceeds 1% within 15 minutes of deploy</li>
  <li>Any P1 incident raised post-deployment</li>
  <li>Key feature unavailable after verification</li>
</ul>
"""


def build_test_evidence(proj: dict, tickets: list) -> str:
    key  = proj["Jira Key"]
    name = proj["Project"]
    ver  = proj["Fix Version"] or ""

    test_tickets = [t for t in tickets if t["Project"] == key
                    and str(t.get("Test Linked?", "")).lower() in ("yes", "true", "1")]
    all_tickets  = [t for t in tickets if t["Project"] == key]
    done_tickets = [t for t in all_tickets if (t["Status"] or "").lower() == "done"]

    test_rows = "".join(
        f"<tr><td>{t['Ticket Key']}</td><td>{t['Summary']}</td>"
        f"<td>{_badge('Linked', '#00875A')}</td>"
        f"<td>{_badge(t['Status'] or '', _status_color(t['Status'] or ''))}</td></tr>"
        for t in test_tickets
    ) or "<tr><td colspan='4'>No test-linked tickets found</td></tr>"

    return f"""
<h1>{name} — Test Evidence</h1>
<p><strong>Version:</strong> {ver}</p>

<h2>Test Coverage Summary</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Metric</th><th>Value</th>
  </tr>
  <tr><td>Total tickets</td><td>{len(all_tickets)}</td></tr>
  <tr><td>Tickets Done</td><td>{len(done_tickets)}</td></tr>
  <tr><td>Test-linked tickets</td><td>{len(test_tickets)}</td></tr>
  <tr><td>Coverage</td>
      <td>{int(len(test_tickets)/len(all_tickets)*100) if all_tickets else 0}%</td></tr>
</table>

<h2>Test-Linked Tickets</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Ticket</th><th>Summary</th><th>Test</th><th>Status</th>
  </tr>
  {test_rows}
</table>

<h2>Test Types Performed</h2>
<ul>
  <li>Unit Tests — per ticket PR</li>
  <li>Integration Tests — staging environment</li>
  <li>Regression Tests — automated suite</li>
  <li>UAT — stakeholder sign-off</li>
</ul>
"""


def build_rollback_procedure(proj: dict) -> str:
    key  = proj["Jira Key"]
    name = proj["Project"]
    ver  = proj["Fix Version"] or ""
    db   = proj["Has DB Migration"] or "No"

    return f"""
<h1>{name} — Rollback Procedure</h1>
<p><strong>Version:</strong> {ver} &nbsp;|&nbsp; <strong>DB Migration:</strong> {db}</p>

<h2>When to Rollback</h2>
<ul>
  <li>Error rate exceeds threshold within 15 minutes of deployment</li>
  <li>P1 incident raised and confirmed as deployment-related</li>
  <li>On-call engineer and release manager agree rollback is needed</li>
</ul>

<h2>Rollback Steps</h2>
<ol>
  <li>Notify stakeholders via incident channel</li>
  <li>Raise ServiceNow incident linked to {key} Change Request</li>
  <li>Revert deployment to previous stable version</li>
  {"<li><strong>Run DB rollback script (migration reverse)</strong></li>" if "Yes" in str(db) else ""}
  <li>Verify application is operational on previous version</li>
  <li>Run smoke tests to confirm stability</li>
  <li>Update ServiceNow CR with rollback outcome</li>
  <li>Schedule post-incident review within 48 hours</li>
</ol>

<h2>Contacts</h2>
<table>
  <tr style="background:#F4F5F7;"><th>Role</th><th>Action</th></tr>
  <tr><td>Release Manager</td><td>Approves rollback decision</td></tr>
  <tr><td>On-Call Engineer</td><td>Executes rollback steps</td></tr>
  <tr><td>CAB Representative</td><td>Updates ServiceNow CR</td></tr>
  <tr><td>QA Lead</td><td>Verifies rollback smoke tests</td></tr>
</table>
"""


def build_release_notes(proj: dict, tickets: list, history: list) -> str:
    key  = proj["Jira Key"]
    name = proj["Project"]
    ver  = proj["Fix Version"] or ""
    desc = proj["Description"]

    proj_tickets  = [t for t in tickets if t["Project"] == key]
    stories       = [t for t in proj_tickets if t["Type"] == "Story"]
    bugs          = [t for t in proj_tickets if t["Type"] == "Bug"]
    tasks         = [t for t in proj_tickets if t["Type"] == "Task"]
    proj_history  = [h for h in history if key in str(h.get("Team/Project", ""))]

    def ticket_rows(lst):
        if not lst:
            return "<li>None</li>"
        return "".join(f"<li><strong>{t['Ticket Key']}</strong> — {t['Summary']} "
                       f"{_badge(t['Priority'] or 'Medium', _status_color(t['Priority'] or ''))}</li>"
                       for t in lst)

    hist_rows = "".join(
        f"<tr><td>{h['Month']}</td><td>{h['Record Type']}</td>"
        f"<td>{h['Key Data Point']}</td><td>{h['Outcome']}</td></tr>"
        for h in proj_history
    ) or "<tr><td colspan='4'>No historical data</td></tr>"

    return f"""
<h1>{name} — Release Notes</h1>
<p><strong>Version:</strong> {ver}</p>
<p>{desc}</p>

<h2>New Features</h2>
<ul>{ticket_rows(stories)}</ul>

<h2>Bug Fixes</h2>
<ul>{ticket_rows(bugs)}</ul>

<h2>Tasks &amp; Improvements</h2>
<ul>{ticket_rows(tasks)}</ul>

<h2>Historical Context</h2>
<table>
  <tr style="background:#F4F5F7;">
    <th>Month</th><th>Type</th><th>Data Point</th><th>Outcome</th>
  </tr>
  {hist_rows}
</table>

<h2>Known Issues</h2>
<ul>
  <li>Refer to open Jira tickets for remaining known issues</li>
</ul>
"""


# ── Page title mapping ─────────────────────────────────────────────────────────

PAGE_VERSIONS = {
    "ATLAS":  "v3.2",
    "BEACON": "v2.0",
    "CIPHER": "v1.5",
    "DELTA":  "v4.1",
}


def get_page_titles(key: str) -> dict:
    v = PAGE_VERSIONS.get(key, "v1.0")
    return {
        "release_plan":     f"{key} {v} – Release Plan",
        "runbook":          f"{key} {v} – Deployment Runbook",
        "test_evidence":    f"{key} {v} – Test Evidence",
        "rollback":         f"{key} {v} – Rollback Procedure",
        "release_notes":    f"{key} {v} – Release Notes",
    }


# ── Main upload logic ──────────────────────────────────────────────────────────

def upload_project(proj: dict, data: dict, dry_run: bool):
    key = proj["Jira Key"]
    print(f"\n  Project : {proj['Project']}  ({key})")
    print(f"  Space   : {key}")

    existing = {} if dry_run else get_existing_pages(key)
    titles   = get_page_titles(key)

    pages = {
        titles["release_plan"]:  build_release_plan(proj, data["tickets"], data["deps"], data["servicenow"]),
        titles["runbook"]:       build_deployment_runbook(proj, data["tickets"]),
        titles["test_evidence"]: build_test_evidence(proj, data["tickets"]),
        titles["rollback"]:      build_rollback_procedure(proj),
        titles["release_notes"]: build_release_notes(proj, data["tickets"], data["history"]),
    }

    for title, html in pages.items():
        upsert_page(key, title, html, existing, dry_run)


def run(project_filter: str = None, dry_run: bool = False):
    _line("=")
    mode = "DRY-RUN (no changes will be made)" if dry_run else "LIVE UPLOAD"
    print(f"  CONFLUENCE PAGE UPLOAD  [{mode}]".center(WIDTH))
    _line("=")

    if not all([DOMAIN, EMAIL, TOKEN]):
        print("\n[ERROR] Missing Atlassian credentials in .env")
        sys.exit(1)

    print(f"\n  Loading Excel: {EXCEL_PATH.name}")
    data = load_excel()
    projects = data["overview"]

    if project_filter:
        projects = [p for p in projects if p["Jira Key"] == project_filter.upper()]
        if not projects:
            print(f"\n[ERROR] Project '{project_filter}' not found in Excel")
            sys.exit(1)

    print(f"  Projects to upload: {len(projects)}")
    print(f"  Pages per project : 5")
    print(f"  Total pages       : {len(projects) * 5}")

    for proj in projects:
        _line("-")
        upload_project(proj, data, dry_run)

    print()
    _line("=")
    print("  UPLOAD COMPLETE".center(WIDTH))
    _line("=")
    print(f"\n  View your spaces at: {DOMAIN}/wiki/spaces\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload Excel data to Confluence")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Preview pages without creating them")
    parser.add_argument("--project",   type=str, default=None,
                        help="Upload one project only (e.g. ATLAS)")
    args = parser.parse_args()

    run(project_filter=args.project, dry_run=args.dry_run)
