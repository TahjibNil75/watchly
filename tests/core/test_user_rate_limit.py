"""`user_rate_limit` counts per signed-in user, and the routes that cost
something beyond the request use it."""

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.core import rate_limit
from app.core.config import settings
from app.monitoring.infra.aws.routes import router as infra_router
from app.monitoring.notifications.routes import router as notifications_router
from app.monitoring.websites.routes import router as websites_router


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, object]]:
    calls: list[tuple[str, str, object]] = []

    async def fake_count(scope: str, client: str, limit: object) -> None:
        calls.append((scope, client, limit))

    monkeypatch.setattr(rate_limit, "_count", fake_count)
    return calls


def _client(user_id: int) -> TestClient:
    app = FastAPI()

    @app.post("/act", dependencies=[rate_limit.user_rate_limit("check_now")])
    async def act() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user_id)
    return TestClient(app)


def test_counts_each_user_under_the_scope(counted: list) -> None:
    assert _client(7).post("/act").status_code == 200
    assert _client(8).post("/act").status_code == 200
    assert [(scope, key) for scope, key, _ in counted] == [
        ("check_now", "user:7"),
        ("check_now", "user:8"),
    ]
    assert counted[0][2] is settings.RATE_LIMIT_CHECK_NOW


def test_off_when_rate_limiting_is_disabled(
    counted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    assert _client(7).post("/act").status_code == 200
    assert counted == []


def test_unknown_scope_is_refused() -> None:
    with pytest.raises(ValueError):
        rate_limit.user_rate_limit("no_such_scope")


@pytest.mark.parametrize(
    ("router", "path"),
    [
        (websites_router, "/monitoring/websites/{website_id}/check"),
        (infra_router, "/monitoring/infra/aws/resources/{resource_id}/check"),
        (notifications_router, "/monitoring/projects/{project_id}/report"),
    ],
)
def test_routes_are_limited(router: APIRouter, path: str) -> None:
    route = next(
        r for r in router.routes if getattr(r, "path", None) == path and "POST" in r.methods
    )
    names = [d.dependency.__qualname__ for d in route.dependencies]
    assert any(name.startswith("user_rate_limit") for name in names), names
