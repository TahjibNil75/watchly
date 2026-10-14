"""`POST /docker/ingest` authenticates before it counts: pushes per host, and
unknown tokens per client address, never a bucket per token sent."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.monitoring.docker import routes
from app.monitoring.docker.ingest import DockerIngestService


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, object]]:
    calls: list[tuple[str, str, object]] = []

    async def fake_count(scope: str, client: str, limit: object) -> None:
        calls.append((scope, client, limit))

    monkeypatch.setattr(routes, "_count", fake_count)
    monkeypatch.setattr(settings, "DOCKER_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    return calls


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def authenticate(self, token: str):
        return SimpleNamespace(id=5) if token == "good" else None

    monkeypatch.setattr(DockerIngestService, "authenticate", authenticate)
    app = FastAPI()
    app.include_router(routes.ingest_router)
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def push(client: TestClient, token: str):
    return client.post("/docker/ingest", headers={"Authorization": f"Bearer {token}"}, content=b"{}")


def test_unknown_tokens_count_against_the_address_not_the_token(
    client: TestClient, counted: list
) -> None:
    assert push(client, "random-1").status_code == 401
    assert push(client, "random-2").status_code == 401
    keys = {(scope, key) for scope, key, _ in counted}
    assert len(keys) == 1
    scope, key = keys.pop()
    assert scope == "docker_auth_failures"
    assert "random" not in key
    assert counted[0][2] is settings.RATE_LIMIT_DOCKER_AUTH_FAILURES


def test_known_token_counts_against_its_host(client: TestClient, counted: list) -> None:
    # The body is not a push, so it ends in 422 after being counted.
    assert push(client, "good").status_code == 422
    assert [(scope, key) for scope, key, _ in counted] == [("docker_ingest", "host:5")]
    assert counted[0][2] is settings.RATE_LIMIT_DOCKER_INGEST


def test_no_header_is_refused_without_counting(client: TestClient, counted: list) -> None:
    assert client.post("/docker/ingest", content=b"{}").status_code == 401
    assert counted == []


def test_nothing_is_counted_when_rate_limiting_is_disabled(
    client: TestClient, counted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    assert push(client, "nope").status_code == 401
    assert push(client, "good").status_code == 422
    assert counted == []
