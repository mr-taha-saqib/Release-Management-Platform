"""Context -> Processor -> Responder pipeline tests (no network, no LLM)."""
import pytest

from mcp.context import ContextStore
from mcp.processor import Processor
from mcp.responder import Responder

SAMPLE_PROJECTS = ["atlas", "beacon", "cipher", "delta"]


def run(project_id):
    return Processor().process(ContextStore.load(project_id))


# ── Loading and validation ───────────────────────────────────────────────────

def test_rejects_unsafe_project_ids():
    with pytest.raises(ValueError, match="Unsafe project_id"):
        ContextStore.load("../etc/passwd")


def test_unknown_project_lists_what_is_available():
    with pytest.raises(FileNotFoundError, match="Available"):
        ContextStore.load("does_not_exist")


def test_all_bundled_sample_projects_are_listed():
    ids = {p["id"] for p in ContextStore.list_all()}
    assert set(SAMPLE_PROJECTS) <= ids


# ── Metrics on a fully known project ─────────────────────────────────────────

def test_task_counts_and_completion(synthetic_project):
    m = run(synthetic_project()).metrics
    assert (m["tasks_total"], m["tasks_done"], m["tasks_in_progress"],
            m["tasks_blocked"], m["tasks_todo"]) == (5, 2, 1, 1, 1)
    assert m["completion_pct"] == 40.0


def test_remaining_work_is_adjusted_by_historical_overrun(synthetic_project):
    m = run(synthetic_project()).metrics
    # Remaining: (16-4) + 16 + 4 = 32h. Done tasks overran by 25% and 0% -> +12.5%.
    assert m["remaining_hours"] == 32.0
    assert m["time_variance_pct"] == 12.5
    assert m["adjusted_remaining_hours"] == 36.0
    assert m["daily_capacity"] == 16.0


def test_a_blocked_task_raises_risk_to_high(synthetic_project):
    m = run(synthetic_project()).metrics
    assert m["risk_level"] == "HIGH"
    assert m["on_track"] is True
    assert [t["id"] for t in m["blocked_tasks"]] == ["T4"]


def test_missed_deadline_is_critical(synthetic_project):
    m = run(synthetic_project(deadline="2000-01-01")).metrics
    assert m["risk_level"] == "CRITICAL"
    assert m["on_track"] is False
    assert m["buffer_days"] < 0


def test_workload_balance(synthetic_project):
    team = {t["name"]: t for t in run(synthetic_project()).metrics["team_analysis"]}
    assert team["Ana"]["load_status"] == "overloaded"
    assert team["Ben"]["load_status"] == "underutilised"


@pytest.mark.parametrize("project_id", SAMPLE_PROJECTS)
def test_sample_projects_process_consistently(project_id):
    m = run(project_id).metrics
    parts = m["tasks_done"] + m["tasks_in_progress"] + m["tasks_blocked"] + m["tasks_todo"]
    assert parts == m["tasks_total"] > 0
    assert 0 <= m["completion_pct"] <= 100
    assert m["risk_level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


# ── Question routing ─────────────────────────────────────────────────────────

def test_responder_requires_processed_context(synthetic_project):
    with pytest.raises(RuntimeError, match="processed"):
        Responder().answer("Any risks?", ContextStore.load(synthetic_project()))


def test_blocker_question_names_the_blocked_task(synthetic_project):
    answer = Responder().answer("Are there any blocked tasks?", run(synthetic_project()))
    assert "T4" in answer
    assert "Migrate payments DB" in answer


def test_workload_question_names_the_overloaded_member(synthetic_project):
    answer = Responder().answer("Who on the team is overloaded?", run(synthetic_project()))
    assert "Ana" in answer


def test_unmatched_question_falls_back_to_a_summary(synthetic_project):
    ctx = run(synthetic_project())
    assert Responder().answer("hmm", ctx) == Responder()._answer_summary(ctx.metrics)
