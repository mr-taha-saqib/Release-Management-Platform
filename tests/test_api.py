"""FastAPI layer tests against the bundled sample data."""
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_lists_projects():
    r = client.get("/projects")
    assert r.status_code == 200
    assert {"atlas", "beacon", "cipher", "delta"} <= {p["id"] for p in r.json()}


def test_project_metrics():
    r = client.get("/projects/atlas")
    assert r.status_code == 200
    body = r.json()
    assert body["project_name"] == "Atlas"
    assert body["tasks_total"] == body["tasks_done"] + body["tasks_in_progress"] \
        + body["tasks_blocked"] + body["tasks_todo"]


def test_unknown_project_is_404():
    assert client.get("/projects/nope").status_code == 404


def test_unsafe_project_id_is_rejected():
    assert client.get("/projects/bad%20id!").status_code == 422


def test_ask_answers_from_project_data():
    r = client.post("/ask", json={"project_id": "atlas", "question": "Are there any blocked tasks?"})
    assert r.status_code == 200
    body = r.json()
    assert body["project_name"] == "Atlas"
    assert body["answer"].strip()


def test_ask_validates_the_question():
    r = client.post("/ask", json={"project_id": "atlas", "question": "?"})
    assert r.status_code == 422
