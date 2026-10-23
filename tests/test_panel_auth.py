import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ttmon.db.migrate import upgrade_to_head
from ttmon.settings import WebConfig, load_config
from ttmon.web.app import create_app
from ttmon.web.guard import (
    AUTH_FAILS_MAX,
    AUTH_WINDOW_S,
    SECURITY_HEADERS,
    UnsafeExposure,
    allowed_hosts,
    basic_auth,
    check_exposure,
)
from ttmon.web.jobs import JobRunner

CFG = load_config()
PASSWORD = "correct-horse-battery"


def _basic(password: str, user: str = "admin") -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_file = tmp_path / "ttmon.db"
    upgrade_to_head(db_file)
    app = create_app(
        CFG,
        db_file,
        tmp_path / "screenshots",
        runner=JobRunner(),
        schedule=False,
        panel_password=PASSWORD,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def test_password_required_everywhere(client: TestClient) -> None:
    for path in ("/", "/api/health", "/api/runs"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"].startswith("Basic")


def test_any_user_with_right_password(client: TestClient) -> None:
    assert client.get("/api/health", headers=_basic(PASSWORD, "кто-угодно")).status_code == 200
    assert client.get("/api/health", headers=_basic("wrong-password-1")).status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Basic !!!"}).status_code == 401
    non_ascii = {b"Authorization": "Basic YWJj\u00e9".encode("latin-1")}
    assert client.get("/api/health", headers=non_ascii).status_code == 401


def test_csrf_still_checked_with_password(client: TestClient) -> None:
    response = client.post(
        "/api/runs/morning",
        headers={
            **_basic(PASSWORD),
            "Origin": "https://evil.example",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 403


def test_security_headers_even_on_401(client: TestClient) -> None:
    response = client.get("/")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


async def test_too_many_wrong_passwords_locks_ip_for_window() -> None:
    now = [0.0]
    app = FastAPI()
    app.middleware("http")(basic_auth(PASSWORD, clock=lambda: now[0]))

    @app.get("/")
    async def index() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as test_client:
        for _ in range(AUTH_FAILS_MAX):
            assert test_client.get("/", headers=_basic("nope-nope-nope")).status_code == 401
        # заблокирован даже правильный пароль, и без заголовка — тоже
        locked = test_client.get("/", headers=_basic(PASSWORD))
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
        assert test_client.get("/").status_code == 429
        now[0] += AUTH_WINDOW_S + 1
        assert test_client.get("/", headers=_basic(PASSWORD)).status_code == 200


async def test_request_without_credentials_is_not_a_failed_attempt() -> None:
    app = FastAPI()
    app.middleware("http")(basic_auth(PASSWORD))

    @app.get("/")
    async def index() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as test_client:
        for _ in range(AUTH_FAILS_MAX + 5):
            assert test_client.get("/").status_code == 401
        assert test_client.get("/", headers=_basic(PASSWORD)).status_code == 200


def _web(**update: object) -> WebConfig:
    return CFG.web.model_copy(update=update)


@pytest.mark.parametrize(
    ("host", "password"),
    [("127.0.0.1", None), ("localhost", None), ("0.0.0.0", PASSWORD), ("127.0.0.1", PASSWORD)],
)
def test_exposure_allowed(host: str, password: str | None) -> None:
    check_exposure(_web(host=host), password)


@pytest.mark.parametrize(
    ("host", "password", "match"),
    [("0.0.0.0", None, "без PANEL_PASSWORD"), ("127.0.0.1", "short", "короче")],
)
def test_exposure_rejected(host: str, password: str | None, match: str) -> None:
    with pytest.raises(UnsafeExposure, match=match):
        check_exposure(_web(host=host), password)


def test_public_url_needs_password_even_on_loopback() -> None:
    with pytest.raises(UnsafeExposure, match="public_url"):
        check_exposure(_web(host="127.0.0.1", public_url="https://panel.example.kz"), None)


def test_unspecified_host_not_trusted_but_public_url_is() -> None:
    hosts = allowed_hosts(_web(host="0.0.0.0", public_url="https://panel.example.kz"))
    assert hosts == ["127.0.0.1", "localhost", "panel.example.kz"]
