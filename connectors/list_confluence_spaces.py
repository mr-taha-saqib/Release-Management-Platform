"""
connectors/list_confluence_spaces.py
=====================================
Lists all Confluence spaces your account can access.
Run this to find the correct CONFLUENCE_SPACE_KEY for your .env.

Usage:
    python connectors/list_confluence_spaces.py
"""

import os
import sys
from pathlib import Path
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

DOMAIN = os.getenv("ATLASSIAN_DOMAIN", "").rstrip("/")
EMAIL  = os.getenv("ATLASSIAN_EMAIL", "")
TOKEN  = os.getenv("ATLASSIAN_API_TOKEN", "")


def list_spaces():
    if not (DOMAIN and EMAIL and TOKEN):
        print("[ERROR] Missing credentials in .env — set ATLASSIAN_DOMAIN, ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN")
        sys.exit(1)

    url  = f"{DOMAIN}/wiki/rest/api/space"
    auth = HTTPBasicAuth(EMAIL, TOKEN)
    hdrs = {"Accept": "application/json"}

    try:
        r = requests.get(url, headers=hdrs, auth=auth,
                         params={"limit": 50, "type": "global"}, timeout=20)
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Cannot reach {DOMAIN} — check your internet connection.")
        sys.exit(1)

    if r.status_code == 401:
        print("[ERROR] 401 Unauthorized — your API token may be expired or wrong.")
        sys.exit(1)

    r.raise_for_status()
    spaces = r.json().get("results", [])

    if not spaces:
        # Also try personal spaces
        r2 = requests.get(url, headers=hdrs, auth=auth,
                          params={"limit": 50, "type": "personal"}, timeout=20)
        spaces = r2.json().get("results", [])

    print()
    print("=" * 55)
    print("  YOUR CONFLUENCE SPACES")
    print("=" * 55)

    if not spaces:
        print("  No spaces found. You may not have any Confluence spaces yet.")
        print()
        print("  To create one:")
        print(f"  -> {DOMAIN}/wiki  -> Create Space")
        print("=" * 55)
        return

    print(f"  {'KEY':<15}  {'NAME'}")
    print("  " + "-" * 50)
    for sp in spaces:
        key  = sp.get("key", "?")
        name = sp.get("name", "?")
        print(f"  {key:<15}  {name}")

    print("=" * 55)
    print()
    print("  Copy the KEY you want and put it in your .env:")
    print("  CONFLUENCE_SPACE_KEY=<KEY>")
    print()


if __name__ == "__main__":
    list_spaces()
