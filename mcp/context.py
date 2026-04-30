"""
mcp/context.py -- Layer 1: Context Store

Responsibility:
  * Load a project JSON file from disk into memory.
  * Validate the structure before passing downstream.
  * Expose the raw data and a human-readable "context window"
    (a structured text summary) that the Responder can include
    in prompts or logs.
  * Cache computed metrics once the Processor has run.

Nothing in this layer does any arithmetic or generates text answers.
It is purely a validated container and data-access interface.

Usage:
    ctx = ContextStore.load("flask")
    ctx = ContextStore.load("alpha")
    print(ctx.project_name)
    print(ctx.task_count)
    print(ctx.context_window())     # human-readable text summary
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

# Root folder where all project JSON files are stored (data/project_<id>.json)
DATA_DIR     = Path(__file__).parent.parent / "data"

# Only allow safe characters in project IDs to prevent path traversal attacks
_SAFE_ID_RE  = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ContextStore:
    """
    Holds all project data for one project.

    This is Layer 1 of the MCP pipeline. It acts as a validated,
    read-only data container. All values come from the JSON file on disk —
    nothing is computed here. The Processor (Layer 2) reads from this and
    writes back computed metrics into self.metrics.

    Attributes (set on load):
        project_id    : str        -- the short ID used to find the file (e.g. "sam1")
        project_name  : str        -- the human-readable project title
        raw_data      : dict       -- the entire JSON as loaded from disk
        confluence_data : dict     -- Confluence pages (loaded if confluence_<id>.json exists)
        metrics       : dict|None  -- set by Processor after analysis; None until then

    The ContextStore is intentionally dumb: it stores and exposes,
    it does not compute.
    """

    def __init__(self):
        # All attributes start empty; use ContextStore.load() to populate them
        self.project_id:    str        = ""
        self.project_name:  str        = ""
        self.raw_data:      dict       = {}
        self.confluence_data:  dict     = {}   # loaded if confluence_<id>.json exists
        self.servicenow_data:  dict     = {}   # loaded if servicenow_snapshot.json exists
        self.metrics:       Any | None = None  # None until Processor.process() runs
        self._source_path:  Path | None = None

    # ── Factory method ─────────────────────────────────────────────────────────

    @classmethod
    def load(cls, project_id: str) -> "ContextStore":
        """
        Load a project from data/project_<project_id>.json.

        This is the only intended way to create a ContextStore.
        It validates the project_id format, reads the JSON file,
        checks for required keys, and optionally loads Confluence data
        from a matching confluence_<project_id>.json file.

        Parameters
        ----------
        project_id : str
            Short identifier for the project (e.g. "sam1", "flask").
            Only letters, digits, hyphens, and underscores are allowed.

        Returns
        -------
        ContextStore
            A fully populated store, ready to be passed to Processor.

        Raises
        ------
        ValueError
            If project_id contains unsafe characters.
        FileNotFoundError
            If data/project_<project_id>.json does not exist.
        ValueError
            If the JSON file is malformed or missing required keys
            (project, tasks, team, project.name, project.deadline).
        """
        # Guard: reject IDs that could be used in path traversal (e.g. "../secrets")
        if not _SAFE_ID_RE.match(project_id):
            raise ValueError(
                f"Unsafe project_id '{project_id}'. "
                "Only letters, digits, hyphens, and underscores are allowed."
            )

        # Locate the JSON file; tell user what is available if missing
        path = DATA_DIR / f"project_{project_id}.json"
        if not path.exists():
            available = cls._available_ids()
            raise FileNotFoundError(
                f"No project file for id '{project_id}'.\n"
                f"Expected: {path}\n"
                f"Available: {available}"
            )

        # Parse JSON — surface a clear error instead of a raw JSONDecodeError
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parse error in {path.name}: {e}")

        # ── Structural validation ──────────────────────────────────────────────
        # All three top-level keys are required for the Processor to run.
        for key in ("project", "tasks", "team"):
            if key not in raw:
                raise ValueError(
                    f"project_{project_id}.json missing required key '{key}'. "
                    f"Found: {list(raw.keys())}"
                )
        # These two project sub-fields are used in every calculation
        for key in ("name", "deadline"):
            if key not in raw["project"]:
                raise ValueError(
                    f"project.{key} is required but missing in {path.name}."
                )
        # tasks and team must be lists so we can iterate and count them
        if not isinstance(raw["tasks"], list):
            raise ValueError(f"'tasks' must be a list in {path.name}.")
        if not isinstance(raw["team"], list):
            raise ValueError(f"'team' must be a list in {path.name}.")

        # ── Populate the store ────────────────────────────────────────────────
        ctx = cls()
        ctx.project_id   = project_id
        ctx.project_name = raw["project"]["name"]
        ctx.raw_data     = raw
        ctx._source_path = path

        # ── Optionally load Confluence data ───────────────────────────────────
        # Confluence is optional — if the file is missing or broken, we
        # silently skip it. The Processor checks has_confluence before using it.
        # Try uppercase key first (confluence_ATLAS.json), then lowercase fallback
        conf_path = DATA_DIR / f"confluence_{project_id.upper()}.json"
        if not conf_path.exists():
            conf_path = DATA_DIR / f"confluence_{project_id}.json"
        if conf_path.exists():
            try:
                ctx.confluence_data = json.loads(conf_path.read_text(encoding="utf-8"))
            except Exception:
                ctx.confluence_data = {}

        # ── Optionally load ServiceNow snapshot ───────────────────────────────
        sn_path = DATA_DIR / "servicenow_snapshot.json"
        if sn_path.exists():
            try:
                ctx.servicenow_data = json.loads(sn_path.read_text(encoding="utf-8"))
            except Exception:
                ctx.servicenow_data = {}

        return ctx

    # ── Convenience accessors ──────────────────────────────────────────────────
    # These are shortcuts so callers don't have to dig into raw_data manually.

    @property
    def project(self) -> dict:
        """Return the 'project' sub-dict (name, deadline, description, etc.)."""
        return self.raw_data["project"]

    @property
    def tasks(self) -> list:
        """Return the full list of task dicts."""
        return self.raw_data["tasks"]

    @property
    def team(self) -> list:
        """Return the full list of team member dicts."""
        return self.raw_data["team"]

    @property
    def task_count(self) -> int:
        """Total number of tasks in the project."""
        return len(self.tasks)

    @property
    def deadline(self) -> str:
        """Deadline string in YYYY-MM-DD format, taken directly from the JSON."""
        return self.project["deadline"]

    @property
    def has_confluence(self) -> bool:
        """
        True if Confluence pages were successfully loaded for this project.
        Used by the Processor to decide whether to run Confluence analysis.
        """
        return bool(self.confluence_data.get("pages"))

    @property
    def is_processed(self) -> bool:
        """
        True once the Processor has populated self.metrics.
        The Responder checks this before answering to avoid using stale/empty data.
        """
        return self.metrics is not None

    @property
    def data_source(self) -> str:
        """
        Human-readable description of where this project's data comes from.

        Reads the optional 'source' dict inside project{} to determine
        whether data was fetched from Jira, GitHub, or is a local JSON file.
        This string is printed in every answer to let the user know
        whether they are looking at live or simulated data.
        """
        src = self.project.get("source", {})
        if src:
            src_type = src.get("type", "")

            # ── Jira Cloud source ──────────────────────────────────────────────
            if "jira" in src_type:
                domain  = src.get("domain", "")
                key     = src.get("project_key", "")
                fetched = src.get("fetched_at", "")
                return (
                    f"Jira Cloud ({domain}/jira/software/projects/{key}) "
                    f"-- live data fetched {fetched}"
                )

            # ── GitHub Issues source ───────────────────────────────────────────
            if "github" in src_type:
                real  = src.get("real_issues", 0)
                synth = src.get("synth_issues", 0)
                url   = f"https://github.com/{src.get('owner','')}/{src.get('repo','')}"
                return (
                    f"GitHub Issues ({url}) "
                    f"-- {real} real API records + {synth} supplemental "
                    f"fetched {src.get('fetched_at','')}"
                )

        # Fallback: no source metadata means this is a hand-crafted local file
        return "Local JSON file (simulated project data)"

    # ── Context window ─────────────────────────────────────────────────────────

    def context_window(self) -> str:
        """
        Return a human-readable text snapshot of the current project state.

        This is the "context" that would be injected into an LLM prompt
        in a real Model Context Protocol setup. Here it is used for
        logging, debugging, and the --context CLI flag.

        The output includes:
          - Task counts grouped by status
          - Team member names
          - Blocked task details
          - Computed metrics (if the Processor has already run)
          - Confluence summary (if pages were loaded)

        Returns
        -------
        str
            A multi-line plain-text summary, suitable for printing or logging.
        """
        t = self.tasks

        # ── Count tasks by status ──────────────────────────────────────────────
        done    = sum(1 for x in t if x.get("status") == "Done")
        in_prog = sum(1 for x in t if x.get("status") == "In Progress")
        blocked = sum(1 for x in t if x.get("status") == "Blocked")
        todo    = sum(1 for x in t if x.get("status") == "To Do")
        total   = len(t)
        pct     = round(done / total * 100, 1) if total else 0.0

        team_names = [m.get("name","?") for m in self.team]

        lines = [
            f"PROJECT CONTEXT: {self.project_name}",
            f"Data source    : {self.data_source}",
            f"Deadline       : {self.deadline}",
            "",
            "Tasks:",
            f"  Total={total}  Done={done}  In-Progress={in_prog}  "
            f"Blocked={blocked}  To-Do={todo}  ({pct}% complete)",
            "",
            "Team:",
            f"  {', '.join(team_names)}",
            "",
            "Blocked tasks:",
        ]
        # List each blocked task with its ID, title, assignee, and estimate
        for task in t:
            if task.get("status") == "Blocked":
                lines.append(
                    f"  [{task.get('id','?')}] {task.get('title','')[:60]}"
                    f"  -- {task.get('assigned_to','?')}"
                    f"  ({task.get('estimated_time',0)}h estimated)"
                )

        # ── Append computed metrics if Processor has already run ───────────────
        if self.is_processed:
            m = self.metrics
            lines += [
                "",
                "Computed metrics (from Processor):",
                f"  Risk level         : {m.get('risk_level','')}",
                f"  Projected finish   : {m.get('projected_finish','')}",
                f"  Buffer days        : {m.get('buffer_days','')}",
                f"  Time variance      : {m.get('time_variance_pct','')}%",
                f"  Adjusted remaining : {m.get('adjusted_remaining_hours','')}h",
            ]

        # ── Append Confluence summary if pages are available ───────────────────
        if self.has_confluence:
            pages    = self.confluence_data.get("pages", [])
            meetings = [p for p in pages if p.get("is_meeting_note")]
            lines += [
                "",
                f"Confluence ({self.confluence_data.get('space_name','')}):",
                f"  Pages: {len(pages)}  |  Meeting notes: {len(meetings)}",
            ]
            # Show the 3 most recent meeting note titles as a quick reference
            for p in meetings[:3]:
                lines.append(f"  [{p['last_updated']}] {p['title']}")

        return "\n".join(lines)

    # ── Listing helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _available_ids() -> list[str]:
        """
        Scan the data/ directory and return a list of known project IDs.
        Used in error messages to tell the user what projects exist.
        """
        ids = []
        for p in sorted(DATA_DIR.glob("project_*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                ids.append(d.get("project", {}).get("id", p.stem.replace("project_","")))
            except Exception:
                continue
        return ids

    @staticmethod
    def list_all() -> list[dict]:
        """
        Return a list of all available projects as [{id, name, file}] dicts.
        Used by the CLI menu and the /projects API endpoint to populate dropdowns.
        """
        projects = []
        for p in sorted(DATA_DIR.glob("project_*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                proj = d.get("project", {})
                projects.append({
                    "id":   proj.get("id", p.stem.replace("project_","")),
                    "name": proj.get("name", p.stem),
                    "file": p.name,
                })
            except Exception:
                continue
        return projects

    def __repr__(self) -> str:
        processed = "processed" if self.is_processed else "raw"
        return (f"<ContextStore id={self.project_id!r} "
                f"name={self.project_name!r} "
                f"tasks={self.task_count} {processed}>")
