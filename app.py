"""
app.py -- FastAPI API for the Local LLM Project Manager.

Run:
    python app.py
    OR
    uvicorn app:app --reload --port 8000

Endpoints:
    GET  /projects                List all available projects (JSON)
    GET  /projects/{id}           Project snapshot metrics (JSON)
    POST /ask                     Ask a question (JSON)
    GET  /context/{id}            MCP context window (plain text)
    POST /sync                    Fetch fresh data from Jira + Confluence
    GET  /confluence/{id}         Confluence pages for a project (JSON)
    GET  /docs                    Swagger UI
    GET  /redoc                   ReDoc API docs
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mcp.context   import ContextStore
from mcp.processor import Processor
from mcp.responder import Responder

DATA_DIR = Path(__file__).parent / "data"

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "Local LLM Project Manager",
    description = (
        "Project intelligence API with MCP pipeline.\n\n"
        "**MCP Pipeline**: Context -> Processor -> Responder\n\n"
        "**Data sources**: Local JSON, Jira Cloud\n\n"
        "Use **POST /sync** to fetch fresh data from Jira."
    ),
    version     = "3.0.0",
)

_processor = Processor()
_responder = Responder()

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _load(project_id: str) -> ContextStore:
    """Run MCP Layers 1 + 2. Raise 404 if project not found."""
    try:
        ctx = ContextStore.load(project_id)
        return _processor.process(ctx)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── Request / Response models ──────────────────────────────────────────────────

class AskRequest(BaseModel):
    project_id: str = Field(..., description="Project ID to query (e.g. 'sam1')", examples=["sam1"])
    question:   str = Field(..., min_length=3, description="Natural-language question", examples=["What are the main risks?"])

class AskResponse(BaseModel):
    project_id:   str
    project_name: str
    question:     str
    answer:       str
    risk_level:   str
    data_source:  str

class ProjectSummary(BaseModel):
    id:   str
    name: str
    file: str

class ProjectMetrics(BaseModel):
    project_id:               str
    project_name:             str
    description:              str
    data_source:              str
    deadline:                 str
    completion_pct:           float
    tasks_total:              int
    tasks_done:               int
    tasks_in_progress:        int
    tasks_blocked:            int
    tasks_todo:               int
    risk_level:               str
    on_track:                 bool
    buffer_days:              int
    projected_finish:         str
    adjusted_remaining_hours: float
    time_variance_pct:        float
    daily_capacity:           float
    risk_factors:             list[str]
    priorities:               list[str]

class SyncResponse(BaseModel):
    status:           str
    message:          str
    project_id:       str
    project_name:     str
    tasks_fetched:    int
    team_members:     int
    saved_path:       str
    confluence_pages: int = 0
    servicenow_crs:   int = 0

class ConfluencePage(BaseModel):
    id:              str
    title:           str
    content:         str
    url:             str
    last_updated:    str
    labels:          list[str]
    is_meeting_note: bool

class ConfluenceResponse(BaseModel):
    space_key:  str
    space_name: str
    fetched_at: str
    page_count: int
    pages:      list[ConfluencePage]


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def serve_ui():
    """Serve the dashboard UI."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/projects", response_model=list[ProjectSummary], tags=["Projects"])
def list_projects():
    """List all available projects."""
    return ContextStore.list_all()


@app.get("/projects/{project_id}", response_model=ProjectMetrics, tags=["Projects"])
def get_project(project_id: str):
    """Return full computed metrics for a single project."""
    ctx = _load(project_id)
    m   = ctx.metrics
    return ProjectMetrics(
        project_id               = m["project_id"],
        project_name             = m["project_name"],
        description              = m["description"],
        data_source              = m["data_source"],
        deadline                 = m["deadline"],
        completion_pct           = m["completion_pct"],
        tasks_total              = m["tasks_total"],
        tasks_done               = m["tasks_done"],
        tasks_in_progress        = m["tasks_in_progress"],
        tasks_blocked            = m["tasks_blocked"],
        tasks_todo               = m["tasks_todo"],
        risk_level               = m["risk_level"],
        on_track                 = m["on_track"],
        buffer_days              = m["buffer_days"],
        projected_finish         = m["projected_finish"],
        adjusted_remaining_hours = m["adjusted_remaining_hours"],
        time_variance_pct        = m["time_variance_pct"],
        daily_capacity           = m["daily_capacity"],
        risk_factors             = m["risk_factors"],
        priorities               = m["priorities"],
    )


@app.post("/ask", response_model=AskResponse, tags=["Q&A"])
def ask(body: AskRequest):
    """
    Ask a natural-language question about a project.

    Example questions:
    - "When will the project be completed?"
    - "What are the main risks?"
    - "Are there any blocked tasks?"
    - "Who is overloaded on the team?"
    - "What should we prioritise next?"
    """
    ctx    = _load(body.project_id)
    answer = _responder.answer(body.question, ctx)
    m      = ctx.metrics
    return AskResponse(
        project_id   = body.project_id,
        project_name = m["project_name"],
        question     = body.question,
        answer       = answer,
        risk_level   = m["risk_level"],
        data_source  = m["data_source"],
    )


@app.get("/context/{project_id}", response_class=PlainTextResponse, tags=["Debug"])
def get_context(project_id: str):
    """Return the MCP Layer 1 context window as plain text."""
    ctx = _load(project_id)
    return ctx.context_window()


@app.post("/sync", response_model=SyncResponse, tags=["Sync"])
def sync_jira():
    """
    Fetch fresh data from Jira + Confluence + ServiceNow.
    Requires credentials in .env file.
    """
    try:
        from connectors.sync import run_sync
        result = run_sync(verbose=False)

        # Count confluence pages synced (sum across all space files)
        conf_pages = 0
        for f in DATA_DIR.glob("confluence_*.json"):
            try:
                import json as _json
                d = _json.loads(f.read_text(encoding="utf-8"))
                conf_pages += len(d.get("pages", []))
            except Exception:
                pass

        # Count ServiceNow CRs from snapshot
        sn_crs = 0
        sn_path = DATA_DIR / "servicenow_snapshot.json"
        if sn_path.exists():
            try:
                import json as _json
                sn = _json.loads(sn_path.read_text(encoding="utf-8"))
                sn_crs = len(sn.get("change_requests", []))
            except Exception:
                pass

        return SyncResponse(
            status           = "ok",
            message          = result["message"],
            project_id       = result["project_id"],
            project_name     = result["project_name"],
            tasks_fetched    = result["tasks_fetched"],
            team_members     = result["team_members"],
            saved_path       = result["saved_path"],
            confluence_pages = conf_pages,
            servicenow_crs   = sn_crs,
        )
    except SystemExit:
        raise HTTPException(status_code=400, detail="Sync failed — check .env credentials. Run python setup_check.py for details.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/servicenow", tags=["ServiceNow"])
def get_servicenow():
    """Return the ServiceNow snapshot plus instance URL for building links."""
    import os
    path = DATA_DIR / "servicenow_snapshot.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No ServiceNow data. Run POST /sync or python connectors/sync.py")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["instance_url"] = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/llm-status", tags=["Debug"])
def llm_status():
    """Return current LLM backend status (Ollama running, model pulled, etc.)."""
    try:
        from src.llm_interface import _ollama_is_running, _ollama_has_model, OLLAMA_MODEL, active_backend_label
        running   = _ollama_is_running()
        has_model = _ollama_has_model() if running else False
        return {
            "ollama_running": running,
            "model_pulled":   has_model,
            "model_name":     OLLAMA_MODEL,
            "active_backend": active_backend_label(),
            "using_llm":      running and has_model,
        }
    except Exception as e:
        return {"ollama_running": False, "model_pulled": False, "active_backend": "simulator", "using_llm": False, "error": str(e)}


@app.get("/confluence/{project_id}", response_model=ConfluenceResponse, tags=["Confluence"])
def get_confluence(project_id: str):
    """Return Confluence pages saved for this project. Run POST /sync first."""
    if not re.match(r"^[A-Za-z0-9_\-]{1,64}$", project_id):
        raise HTTPException(status_code=422, detail="Invalid project_id")

    path = DATA_DIR / f"confluence_{project_id.upper()}.json"
    if not path.exists():
        path = DATA_DIR / f"confluence_{project_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No Confluence data for '{project_id}'. Run POST /sync with CONFLUENCE_SPACE_KEY set in .env first.")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading Confluence data: {e}")

    pages = [ConfluencePage(**p) for p in data.get("pages", [])]
    return ConfluenceResponse(
        space_key  = data.get("space_key", ""),
        space_name = data.get("space_name", ""),
        fetched_at = data.get("fetched_at", ""),
        page_count = len(pages),
        pages      = pages,
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 56)
    print("  LLM Project Manager  --  FastAPI")
    print("  http://localhost:8000/docs     (Swagger)")
    print("  http://localhost:8000/redoc    (ReDoc)")
    print("=" * 56)
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
