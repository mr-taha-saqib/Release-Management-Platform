import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp.context  # noqa: E402
import mcp.responder  # noqa: E402


@pytest.fixture(autouse=True)
def no_ollama(monkeypatch):
    """Force the rule-based responder: never reach for a local Ollama server."""
    monkeypatch.setattr(mcp.responder, "_try_ollama", lambda question, context: None)


def _task(tid, status, hours, actual=None, title=None):
    return {"id": tid, "title": title or f"Task {tid}", "status": status, "priority": "High",
            "assigned_to": "", "estimated_time": hours, "actual_time": actual}


@pytest.fixture
def synthetic_project(tmp_path, monkeypatch):
    """Writes a small, fully known project and points the loader at it."""

    def make(deadline="2099-12-31"):
        data = {
            "project": {"id": "demo", "name": "Demo Release", "deadline": deadline},
            "tasks": [
                _task("T1", "Done", 8, actual=10),
                _task("T2", "Done", 8, actual=8),
                _task("T3", "In Progress", 16, actual=4),
                _task("T4", "Blocked", 16, title="Migrate payments DB"),
                _task("T5", "To Do", 4),
            ],
            "team": [
                {"id": "ana", "name": "Ana", "role": "Backend", "capacity_hours_per_week": 40,
                 "assigned_tasks": ["T1", "T2", "T3", "T4"]},
                {"id": "ben", "name": "Ben", "role": "QA", "capacity_hours_per_week": 40,
                 "assigned_tasks": ["T5"]},
            ],
        }
        (tmp_path / "project_demo.json").write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(mcp.context, "DATA_DIR", tmp_path)
        return "demo"

    return make
