"""
connectors/confluence_connector.py
===================================
Fetches pages from your Confluence Cloud space and returns them
in a simple format the pipeline can display.

Credentials come from the .env file in the project root.

Main function:
    fetch_confluence_pages()  ->  dict
"""

import os
import re
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# Load .env from project root
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

# ── Read credentials from environment ─────────────────────────────────────────
DOMAIN     = os.getenv("ATLASSIAN_DOMAIN", "").rstrip("/")
EMAIL      = os.getenv("ATLASSIAN_EMAIL", "")
TOKEN      = os.getenv("ATLASSIAN_API_TOKEN", "")
SPACE_KEY  = os.getenv("CONFLUENCE_SPACE_KEY", "").strip()
MAX_PAGES  = 50


def get_space_keys() -> list:
    """
    Read Confluence space keys from .env.
    Supports both:
        CONFLUENCE_SPACE_KEYS=AT,BE,CI,DE   (multiple, comma-separated)
        CONFLUENCE_SPACE_KEY=AT             (single, legacy)
    """
    multi = os.getenv("CONFLUENCE_SPACE_KEYS", "").strip()
    if multi:
        return [k.strip() for k in multi.split(",") if k.strip()]
    single = os.getenv("CONFLUENCE_SPACE_KEY", "").strip()
    if single:
        return [single]
    return []


def _is_configured(space_key: str = None) -> bool:
    """Return True if Confluence credentials and space key are all set."""
    key = space_key or SPACE_KEY
    return bool(DOMAIN and EMAIL and TOKEN and key)


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace into readable plain text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&amp;",  "&",  text)
    text = re.sub(r"&lt;",   "<",  text)
    text = re.sub(r"&gt;",   ">",  text)
    text = re.sub(r"&nbsp;", " ",  text)
    text = re.sub(r"\s+",    " ",  text)
    return text.strip()


def _get(path: str, params: dict = None, space_key: str = None) -> dict:
    """Make one authenticated GET request to the Confluence REST API."""
    url  = f"{DOMAIN}/wiki/rest/api{path}"
    auth = HTTPBasicAuth(EMAIL, TOKEN)
    hdrs = {"Accept": "application/json"}

    try:
        r = requests.get(url, headers=hdrs, auth=auth,
                         params=params or {}, timeout=20)
    except requests.exceptions.ConnectionError:
        print(f"\n[ERROR] Cannot connect to Confluence at {DOMAIN}")
        import sys; sys.exit(1)

    if r.status_code == 401:
        raise PermissionError("Confluence: 401 Unauthorized — check your API token in .env.")
    if r.status_code == 404:
        key = space_key or SPACE_KEY
        raise FileNotFoundError(
            f"Confluence: Space '{key}' not found. "
            "Run: python connectors/list_confluence_spaces.py  to see available keys."
        )

    r.raise_for_status()
    return r.json()


def fetch_confluence_pages(verbose: bool = True, space_key: str = None) -> dict:
    """
    Fetch all pages from the configured Confluence space.

    Returns:
        {
            "space_key":  "MYPROJ",
            "space_name": "My Project",
            "fetched_at": "2026-04-01",
            "pages": [
                {
                    "id":           "12345",
                    "title":        "Sprint 3 Planning",
                    "content":      "plain text, first 2000 chars",
                    "url":          "https://...",
                    "last_updated": "2026-03-28",
                    "labels":       ["sprint", "planning"],
                    "is_meeting_note": True,
                },
                ...
            ]
        }
    """
    key = space_key or SPACE_KEY

    if not _is_configured(key):
        print("  [Confluence] No space key set — skipping.")
        return {}

    if verbose:
        print(f"  Space key : {key}")

    # ── Fetch space info ───────────────────────────────────────────────────────
    space_data = _get(f"/space/{key}", space_key=key)
    space_name = space_data.get("name", key)

    if verbose:
        print(f"  Space name: {space_name}")

    # ── Fetch pages ────────────────────────────────────────────────────────────
    raw = _get("/content", {
        "spaceKey": key,
        "type":     "page",
        "limit":    MAX_PAGES,
        "expand":   "body.storage,version,metadata.labels",
    }, space_key=key)

    pages = []
    meeting_keywords = ["meeting", "standup", "stand-up", "retro",
                        "retrospective", "sprint", "planning", "review",
                        "demo", "kickoff", "notes"]

    for p in raw.get("results", []):
        html    = (p.get("body") or {}).get("storage", {}).get("value", "")
        content = _strip_html(html)[:2000]
        labels  = [lb["name"] for lb in
                   (p.get("metadata") or {}).get("labels", {}).get("results", [])]
        title   = p.get("title", "")

        is_meeting = any(kw in title.lower() for kw in meeting_keywords) or \
                     any(kw in labels for kw in meeting_keywords)

        page_url = f"{DOMAIN}/wiki/spaces/{key}/pages/{p['id']}"

        pages.append({
            "id":             p["id"],
            "title":          title,
            "content":        content,
            "url":            page_url,
            "last_updated":   (p.get("version") or {}).get("when", "")[:10],
            "labels":         labels,
            "is_meeting_note": is_meeting,
        })

    if verbose:
        meetings = sum(1 for p in pages if p["is_meeting_note"])
        print(f"  Pages fetched     : {len(pages)}")
        print(f"  Meeting note pages: {meetings}")

    return {
        "space_key":  key,
        "space_name": space_name,
        "fetched_at": datetime.today().strftime("%Y-%m-%d"),
        "pages":      pages,
    }
