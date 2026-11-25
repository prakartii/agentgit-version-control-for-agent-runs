"""Token protection. These need no database: the check runs before any query."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agentgit_api.deps import get_index
from agentgit_api.main import app


class _EmptyIndex:
    """Stands in for the database so these tests never open a connection."""

    def list_runs(self, limit: int) -> list[object]:
        return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AGENTGIT_API_TOKEN", "s3cret")
    app.dependency_overrides[get_index] = lambda: _EmptyIndex()
    return TestClient(app)


def test_requests_without_a_token_are_rejected(client: TestClient) -> None:
    response = client.get("/runs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_wrong_token_is_rejected(client: TestClient) -> None:
    assert client.get("/runs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/runs", headers={"Authorization": "Basic s3cret"}).status_code == 401


def test_the_right_token_gets_past_authentication(client: TestClient) -> None:
    response = client.get("/runs", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200 and response.json() == []


def test_health_stays_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_no_token_configured_means_no_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGIT_API_TOKEN", raising=False)
    app.dependency_overrides[get_index] = lambda: _EmptyIndex()
    assert TestClient(app).get("/runs").status_code == 200


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()
