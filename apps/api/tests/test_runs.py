from fastapi.testclient import TestClient

from agentgit_api.deps import get_index, get_repo
from agentgit_api.main import app
from agentgit_core.objects import Run, Step


def seed() -> str:
    repo, index = get_repo(), get_index()
    result = repo.put_blob(b"data")
    step = repo.put_step(Step(index=0, kind="tool_call", tool_name="read", result=result))
    run_id = repo.put_run(Run(steps=(step,), metadata={"agent": "demo"}))
    index.set_ref("main", run_id)
    return run_id


def test_list_runs(api_env: None) -> None:
    run_id = seed()
    response = TestClient(app).get("/runs")
    assert response.status_code == 200
    assert [r["id"] for r in response.json()] == [run_id]


def test_get_run_by_ref(api_env: None) -> None:
    run_id = seed()
    body = TestClient(app).get("/runs/main").json()
    assert body["id"] == run_id
    assert body["steps"][0]["tool_name"] == "read"


def test_unknown_run_is_404(api_env: None) -> None:
    assert TestClient(app).get("/runs/nope").status_code == 404


def test_limit_is_validated(api_env: None) -> None:
    assert TestClient(app).get("/runs?limit=0").status_code == 422
