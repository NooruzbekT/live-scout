"""Настоящий Chrome (headless, временный профиль). Запросы страниц перехватываются route:
/live отдаётся из HTML-констант ниже, остальное — abort. Один браузер на модуль:
закрытие Chrome на Windows занимает до десятков секунд."""

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import BrowserContext, Page, Route

from ttmon.collector import selectors as sel
from ttmon.collector.browser import (
    BrowserLaunchError,
    LoginState,
    has_session_cookie,
    login_state,
    open_browser,
)
from ttmon.collector.guards import PageState, StatusTracker, classify, probe
from ttmon.settings import AppConfig, load_config

pytestmark = pytest.mark.asyncio(loop_scope="module")

READY = '<button data-e2e="nav-more-menu">Еще</button>'
LOGGED_OUT_HTML = (
    f'<html><body>{READY}<button id="header-login-button">Войти</button></body></html>'
)
LOGGED_IN_HTML = f"<html><body>{READY}</body></html>"
LATE_LOGIN_HTML = f"""<html><body>{READY}<script>
setTimeout(() => {{
  const b = document.createElement("button");
  b.id = "header-login-button"; b.textContent = "Войти"; document.body.appendChild(b);
}}, 300);
</script></body></html>"""
CAPTCHA_HTML = f"""<html><body>{READY}
<div id="captcha-verify-container-main-page">Перетащите ползунок</div></body></html>"""
HIDDEN_CAPTCHA_HTML = f"""<html><body>{READY}
<div class="captcha_preload" style="display:none">x</div></body></html>"""
FORBIDDEN_HTML = "<html><body><h1>Доступ к www.tiktok.com запрещен</h1></body></html>"
FORBIDDEN_YO_HTML = "<html><body><h1>Доступ к www.tiktok.com запрещён</h1></body></html>"
EMPTY_HTML = "<html><body></body></html>"

Serve = Callable[[str, int], Awaitable[None]]


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    base = load_config()
    browser = base.browser.model_copy(
        update={"profile_dir": tmp_path_factory.mktemp("profile"), "headless": True}
    )
    guards = base.guards.model_copy(
        update={
            "page_timeout_seconds": 3,
            "login_settle_seconds": 1,
            "network_retries": 2,
            "network_retry_pause_seconds": 0,
        }
    )
    paths = base.paths.model_copy(update={"logs_dir": tmp_path_factory.mktemp("logs")})
    return base.model_copy(update={"browser": browser, "guards": guards, "paths": paths})


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared_context(config: AppConfig) -> AsyncIterator[BrowserContext]:
    try:
        async with open_browser(config) as ctx:
            yield ctx
    except BrowserLaunchError as exc:
        pytest.skip(f"Chrome недоступен: {exc}")


@pytest_asyncio.fixture(loop_scope="module")
async def context(shared_context: BrowserContext) -> AsyncIterator[BrowserContext]:
    yield shared_context
    await shared_context.unroute_all(behavior="ignoreErrors")
    await shared_context.clear_cookies()


@pytest_asyncio.fixture(loop_scope="module")
async def page(context: BrowserContext) -> AsyncIterator[Page]:
    p = await context.new_page()
    yield p
    await p.close()


@pytest.fixture
def serve(context: BrowserContext) -> Serve:
    async def _serve(html: str, status: int = 200) -> None:
        async def handler(route: Route) -> None:
            if route.request.url.rstrip("/") == sel.LIVE_URL:
                await route.fulfill(
                    status=status, body=html, content_type="text/html; charset=utf-8"
                )
            else:
                await route.abort()

        await context.route("**/*", handler)

    return _serve


async def _add_session_cookie(context: BrowserContext) -> None:
    await context.add_cookies(
        [{"name": sel.SESSION_COOKIE, "value": "x", "domain": ".tiktok.com", "path": "/"}]
    )


async def test_session_cookie(context: BrowserContext) -> None:
    assert await has_session_cookie(context) is False
    await _add_session_cookie(context)
    assert await has_session_cookie(context) is True


async def test_logged_in(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig
) -> None:
    await serve(LOGGED_IN_HTML, 200)
    await _add_session_cookie(context)
    assert await login_state(context, page, config) == LoginState.LOGGED_IN


async def test_late_login_button_is_logged_out(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig
) -> None:
    await serve(LATE_LOGIN_HTML, 200)
    await _add_session_cookie(context)
    assert await login_state(context, page, config) == LoginState.LOGGED_OUT


async def test_page_not_ready_is_unknown(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig
) -> None:
    await serve(EMPTY_HTML, 200)
    await _add_session_cookie(context)
    assert await login_state(context, page, config) == LoginState.UNKNOWN


async def test_network_error_retried_then_unknown(
    context: BrowserContext, page: Page, config: AppConfig
) -> None:
    attempts = 0

    async def handler(route: Route) -> None:
        nonlocal attempts
        if route.request.url.rstrip("/") == sel.LIVE_URL:
            attempts += 1
        await route.abort("internetdisconnected")

    await context.route("**/*", handler)
    assert await login_state(context, page, config) == LoginState.UNKNOWN
    assert attempts == config.guards.network_retries


@pytest.mark.parametrize("with_cookie", [True, False])
async def test_login_button_means_logged_out(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig, with_cookie: bool
) -> None:
    await serve(LOGGED_OUT_HTML, 200)
    if with_cookie:
        await _add_session_cookie(context)
    assert await login_state(context, page, config) == LoginState.LOGGED_OUT


async def test_no_cookie_means_logged_out(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig
) -> None:
    await serve(LOGGED_IN_HTML, 200)
    assert await login_state(context, page, config) == LoginState.LOGGED_OUT


@pytest.mark.parametrize(
    ("html", "status"),
    [(LOGGED_IN_HTML, 403), (CAPTCHA_HTML, 200)],
    ids=["http-403", "captcha"],
)
async def test_blocked_page_is_unknown_with_screenshot(
    context: BrowserContext, page: Page, serve: Serve, config: AppConfig, html: str, status: int
) -> None:
    await serve(html, status)
    await _add_session_cookie(context)
    errors_dir = Path(config.paths.logs_dir) / "errors"
    before = set(errors_dir.glob("*.jpg")) if errors_dir.exists() else set()

    assert await login_state(context, page, config) == LoginState.UNKNOWN
    assert len(set(errors_dir.glob("*.jpg")) - before) >= 1


@pytest.mark.parametrize(
    ("html", "status", "state"),
    [
        (LOGGED_IN_HTML, 200, PageState.OK),
        (LOGGED_IN_HTML, 404, PageState.ENDED),
        (LOGGED_OUT_HTML, 200, PageState.LOGGED_OUT),
        (CAPTCHA_HTML, 200, PageState.CAPTCHA),
        (HIDDEN_CAPTCHA_HTML, 200, PageState.OK),
        (FORBIDDEN_HTML, 200, PageState.FORBIDDEN),
        (FORBIDDEN_YO_HTML, 200, PageState.FORBIDDEN),
        (LOGGED_IN_HTML, 403, PageState.FORBIDDEN),
    ],
    ids=[
        "ok",
        "http-404",
        "logged-out",
        "captcha",
        "hidden-captcha",
        "forbidden-text",
        "forbidden-yo",
        "http-403",
    ],
)
async def test_probe_classify(
    page: Page, serve: Serve, config: AppConfig, html: str, status: int, state: PageState
) -> None:
    await serve(html, status)
    tracker = StatusTracker(page)
    await page.goto(sel.LIVE_URL)
    assert classify(await probe(page, tracker, config.guards.probe_timeout_seconds)) == state
