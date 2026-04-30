"""
connectors/sync.py
==================
Orchestrates Jira + Confluence + ServiceNow fetching and saves
everything to the data/ folder so the MCP pipeline can use it.

Supports multiple Jira projects via JIRA_PROJECT_KEYS in .env:
    JIRA_PROJECT_KEYS=SAM1,MOB,EC,CRM

Run this script whenever you want to refresh data from all sources:

    python connectors/sync.py

After syncing:
    python main.py                      (CLI)
    python app.py                       (Web API at http://localhost:8000)
"""

import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure project root is on sys.path so imports work from any directory
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

DATA_DIR = _ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

WIDTH = 62


def _line(char="-"):
    print(char * WIDTH)


def _header():
    _line("=")
    print("  JIRA + CONFLUENCE SYNC".center(WIDTH))
    print("  Fetching live data into the MCP pipeline".center(WIDTH))
    _line("=")
    print()


def _save_json(path: Path, data: dict):
    """Write data as pretty-printed JSON to path."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_project_keys() -> list:
    """
    Read project keys from .env.
    Supports both:
        JIRA_PROJECT_KEYS=SAM1,MOB,EC    (multiple, comma-separated)
        JIRA_PROJECT_KEY=SAM1            (single, legacy)
    """
    # Multi-project (new)
    multi = os.getenv("JIRA_PROJECT_KEYS", "").strip()
    if multi:
        return [k.strip().upper() for k in multi.split(",") if k.strip()]

    # Single project (legacy)
    single = os.getenv("JIRA_PROJECT_KEY", "").strip().upper()
    if single:
        return [single]

    return []


def sync_one_project(project_key: str, verbose: bool = True) -> dict:
    """
    Fetch one Jira project by key and save it to data/.
    Returns a result dict.
    """
    from connectors.jira_connector import fetch_jira_project

    # Temporarily override PROJECT_KEY in the connector
    os.environ["JIRA_PROJECT_KEY"] = project_key

    # Re-import to pick up new env var
    import importlib
    import connectors.jira_connector as jc
    jc.PROJECT_KEY = project_key

    if verbose:
        print(f"  Connecting to : {jc.DOMAIN}")
        print(f"  Project key   : {project_key}")
        print()

    jira_data = fetch_jira_project(verbose=verbose)

    project_id   = jira_data["project"]["id"]
    project_name = jira_data["project"]["name"]
    n_tasks      = len(jira_data["tasks"])
    n_team       = len(jira_data["team"])

    jira_path = DATA_DIR / f"project_{project_id}.json"
    _save_json(jira_path, jira_data)

    if verbose:
        print(f"  Saved : data/project_{project_id}.json")
        print()

    return {
        "project_id":    project_id,
        "project_name":  project_name,
        "tasks_fetched": n_tasks,
        "team_members":  n_team,
        "message":       f"Synced {n_tasks} issues -> data/project_{project_id}.json",
        "saved_path":    str(jira_path.relative_to(_ROOT)),
    }


def run_sync(verbose: bool = True) -> dict:
    """
    Fetch all configured Jira projects and save to data/ folder.

    Reads JIRA_PROJECT_KEYS (comma-separated) or JIRA_PROJECT_KEY from .env.

    Returns result of the first/last project synced (for API compatibility).
    """
    keys = _get_project_keys()

    if not keys:
        print("\n[ERROR] No project keys found in .env")
        print("  Set JIRA_PROJECT_KEYS=SAM1,MOB,EC  or  JIRA_PROJECT_KEY=SAM1\n")
        sys.exit(1)

    results = []
    total_projects = len(keys)

    for i, key in enumerate(keys, 1):
        if verbose:
            _line("=")
            print(f"  PROJECT {i} of {total_projects}:  {key}".center(WIDTH))
            _line("=")
            print()

        try:
            result = sync_one_project(key, verbose=verbose)
            results.append(result)
        except SystemExit:
            if verbose:
                print(f"  [SKIP] {key} — check credentials or project key\n")
        except Exception as exc:
            if verbose:
                print(f"  [ERROR] {key}: {exc}\n")

    # ── Confluence (optional, supports multiple space keys) ───────────────────
    from connectors.confluence_connector import get_space_keys, fetch_confluence_pages
    conf_keys = get_space_keys()
    if conf_keys:
        if verbose:
            print()
            _line("-")
            print(f"  Fetching Confluence pages ({len(conf_keys)} space(s)) ...")
        for space_key in conf_keys:
            try:
                conf_data = fetch_confluence_pages(verbose=verbose, space_key=space_key)
                if conf_data:
                    # Save as confluence_{SPACEKEY}.json  (e.g. confluence_AT.json)
                    safe_key  = space_key.lstrip("~")[:20]
                    conf_path = DATA_DIR / f"confluence_{safe_key}.json"
                    _save_json(conf_path, conf_data)
                    if verbose:
                        print(f"  Saved : data/confluence_{safe_key}.json  ({len(conf_data.get('pages', []))} pages)")
            except Exception as exc:
                if verbose:
                    print(f"  [Warning] Confluence sync failed for '{space_key}': {exc}")
    elif verbose:
        print()
        _line("-")
        print("  Confluence — skipped")
        print("  (Set CONFLUENCE_SPACE_KEYS=AT,BE,CI in .env to enable)")

    # ── ServiceNow (optional) ──────────────────────────────────────────────────
    # ServiceNow provides Change Request status, CAB decisions, CMDB CI records,
    # and any incidents linked to the active CRs. Set the three SN_ env vars
    # in .env to enable. Data is saved as data/servicenow_snapshot.json.
    sn_instance = os.getenv("SERVICENOW_INSTANCE", "").strip()
    if sn_instance and results:
        if verbose:
            print()
            _line("-")
            print("  Fetching ServiceNow data ...")
        try:
            from connectors.servicenow_connector import fetch_servicenow_data
            # Use the Jira project keys as ServiceNow search terms.
            # The connector searches for CRs whose short_description contains
            # any of these keys (e.g. "ATLAS", "BEACON").
            sn_data = fetch_servicenow_data(keys, verbose=verbose)
            if sn_data:
                sn_path = DATA_DIR / "servicenow_snapshot.json"
                _save_json(sn_path, sn_data)
                if verbose:
                    print(f"  Saved : data/servicenow_snapshot.json")
                    print(f"          CRs={len(sn_data.get('change_requests', []))}, "
                          f"Tasks={len(sn_data.get('change_tasks', []))}, "
                          f"CIs={len(sn_data.get('cmdb_cis', []))}, "
                          f"Incidents={len(sn_data.get('incidents', []))}")
        except Exception as exc:
            if verbose:
                print(f"  [Warning] ServiceNow sync failed: {exc}")
    elif verbose:
        print()
        _line("-")
        print("  ServiceNow — skipped")
        print("  (Set SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD in .env to enable)")

    # ── Final summary ──────────────────────────────────────────────────────────
    if verbose:
        print()
        _line("=")
        print("  SYNC COMPLETE".center(WIDTH))
        _line("=")
        print(f"  Projects synced : {len(results)} of {total_projects}")
        print()
        for r in results:
            print(f"  [{r['project_id']}]  {r['project_name']}")
            print(f"        Issues : {r['tasks_fetched']}  |  "
                  f"Team : {r['team_members']}  |  "
                  f"File : {r['saved_path']}")
            print()
        _line("-")
        print()
        print("  What to do next:")
        print()
        print("  Option A -- Web UI (browser):")
        print("    python app.py")
        print("    Then open: http://localhost:8000")
        print()
        print("  Option B -- CLI:")
        for r in results:
            print(f"    python main.py --project {r['project_id']}")
        print()
        _line("=")

    # Return last result for API compatibility
    return results[-1] if results else {
        "project_id": "", "project_name": "", "tasks_fetched": 0,
        "team_members": 0, "message": "No projects synced", "saved_path": "",
    }


# ── Run standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _header()
    try:
        run_sync(verbose=True)
    except SystemExit:
        pass
    except Exception as exc:
        print(f"\n[ERROR] Unexpected error during sync:")
        print(f"        {exc}")
        print("\nIf you need help, check setup_check.py first:")
        print("    python setup_check.py\n")
        sys.exit(1)
