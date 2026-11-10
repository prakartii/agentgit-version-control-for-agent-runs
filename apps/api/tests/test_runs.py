from fastapi.testclient import TestClient

from agentgit_api.deps import get_index, get_repo
from agentgit_api.main import app
from agentgit_core.objects import Run, Step


def seed() -> str:
    repo, index = get_repo(), get_index()
    result = repo.put_blob(b"data")
    step = repo.put_step(Step(method="tools/call", tool_name="read", result=result))
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


def test_incomplete_run_is_reported(api_env: None) -> None:
    repo, index = get_repo(), get_index()
    step = repo.put_step(Step(method="tools/call", tool_name="t", outcome="no_response"))
    run_id = repo.put_run(Run(steps=(step,), status="aborted", dropped_steps=1))
    index.set_ref("broken", run_id)
    body = TestClient(app).get("/runs/broken").json()
    assert body["status"] == "aborted"
    assert body["dropped_steps"] == 1
    assert body["steps"][0]["outcome"] == "no_response"


def test_step_detail_decodes_payloads(api_env: None) -> None:
    repo, index = get_repo(), get_index()
    args = repo.put_blob(b'{"path": "a.txt"}')
    result = repo.put_blob(b'{"content": [{"type": "text", "text": "hi"}]}')
    step = repo.put_step(Step(method="tools/call", tool_name="read", arguments=args, result=result))
    index.set_ref("d", repo.put_run(Run(steps=(step,))))
    client = TestClient(app)
    body = client.get("/runs/d/steps/0").json()
    assert body["arguments"] == {"path": "a.txt"}
    assert body["result"]["content"][0]["text"] == "hi"
    assert client.get("/runs/d/steps/5").status_code == 404


def test_diff_reports_a_changed_result(api_env: None) -> None:
    repo, index = get_repo(), get_index()

    def run_with(text: bytes) -> str:
        step = repo.put_step(
            Step(method="tools/call", tool_name="read", result=repo.put_blob(text))
        )
        return repo.put_run(Run(steps=(step,)))

    index.set_ref("a", run_with(b'{"v": 1}'))
    index.set_ref("b", run_with(b'{"v": 2}'))
    body = TestClient(app).get("/diff", params={"a": "a", "b": "b"}).json()
    assert body["identical"] is False
    assert body["first_divergence"] == 0
    change = body["steps"][0]["changes"][0]
    assert (change["path"], change["before"], change["after"]) == ("result.v", 1, 2)
    same = TestClient(app).get("/diff", params={"a": "a", "b": "a"}).json()
    assert same["identical"] is True
