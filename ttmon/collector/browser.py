import contextlib
import subprocess
import sys
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from ttmon.collector import selectors as sel
from ttmon.collector.guards import (
    PageState,
    StatusTracker,
    classify,
    probe,
    save_error_screenshot,
)
from ttmon.settings import AppConfig, project_path

log = structlog.get_logger(__name__)


class LoginState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    # 403 / капча / сеть / страница не отрисовалась: по временной ошибке «перелогиньтесь» не шлём
    UNKNOWN = "unknown"


class BrowserLaunchError(RuntimeError):
    pass


SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def prepare_profile(profile: Path) -> Path:
    """Linux: Chrome, убитый вместе с контейнером, оставляет SingletonLock (симлинк host-pid),
    и на bind-mount с диска Windows следующий Chrome на нём виснет до таймаута. Вызывается
    под collect.lock — живого Chrome на профиле в этот момент нет."""
    profile.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        for name in SINGLETON_FILES:
            (profile / name).unlink(missing_ok=True)
    return profile


@contextlib.asynccontextmanager
async def open_browser(
    cfg: AppConfig, *, headless: bool | None = None
) -> AsyncIterator[BrowserContext]:
    profile = prepare_profile(project_path(cfg.browser.profile_dir))
    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=profile,
                channel=cfg.browser.channel,
                headless=cfg.browser.headless if headless is None else headless,
                viewport={
                    "width": cfg.browser.viewport.width,
                    "height": cfg.browser.viewport.height,
                },
                locale=cfg.browser.locale,
                timezone_id=cfg.timezone,
                args=["--mute-audio"],
            )
        except PlaywrightError as exc:
            first_line = exc.message.splitlines()[0] if exc.message else repr(exc)
            raise BrowserLaunchError(
                f"Chrome не запустился (профиль {profile} уже открыт другим окном?): {first_line}"
            ) from exc
        try:
            yield context
        finally:
            with contextlib.suppress(PlaywrightError):
                await context.close()


async def first_page(context: BrowserContext) -> Page:
    return context.pages[0] if context.pages else await context.new_page()


async def has_session_cookie(context: BrowserContext) -> bool:
    cookies = await context.cookies(sel.BASE_URL)
    return any(c.get("name") == sel.SESSION_COOKIE and c.get("value") for c in cookies)


async def _open_live(page: Page, cfg: AppConfig) -> None:
    """Любая ошибка загрузки или таймаут ожидания шапки повторяются (в том числе на странице
    403/капчи, где шапки нет); итог разбирает classify() по последней попытке."""
    guards = cfg.guards
    timeout_ms = guards.page_timeout_seconds * 1000
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(guards.network_retries),
        wait=wait_fixed(guards.network_retry_pause_seconds),
        retry=retry_if_exception_type(PlaywrightError),
        reraise=True,
    ):
        with attempt:
            if attempt.retry_state.attempt_number > 1:
                log.warning("live_page_retry", attempt=attempt.retry_state.attempt_number)
            await page.goto(sel.LIVE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.locator(f"{sel.LOGIN_BUTTON}, {sel.PAGE_READY}").first.wait_for(
                state="visible", timeout=timeout_ms
            )


async def _late_login_button(page: Page, settle_s: float) -> None:
    # шапка гидрируется после nav-more-menu: кнопка «Войти» может появиться позже
    with contextlib.suppress(PlaywrightTimeoutError):
        await (
            page.locator(sel.LOGIN_BUTTON)
            .filter(visible=True)
            .first.wait_for(state="visible", timeout=settle_s * 1000)
        )


async def login_check(
    context: BrowserContext, page: Page, cfg: AppConfig
) -> tuple[LoginState, PageState]:
    """Залогинен = кука сессии есть И кнопки «Войти» нет: кука не зависит от вёрстки,
    кнопка подтверждает, что сервер сессию не отозвал. PageState — причина для UNKNOWN."""
    tracker = StatusTracker(page)
    try:
        await _open_live(page, cfg)
        await _late_login_button(page, cfg.guards.login_settle_seconds)
        state = classify(await probe(page, tracker, cfg.guards.probe_timeout_seconds))
        has_cookie = await has_session_cookie(context)
    except PlaywrightError as exc:
        log.warning("login_check_failed", status=tracker.status, error=str(exc).splitlines()[0])
        await _error_screenshot(page, cfg, PageState.TIMEOUT)
        return LoginState.UNKNOWN, PageState.TIMEOUT

    log.info("login_check", page_state=state, has_session_cookie=has_cookie)
    match state:
        case PageState.OK:
            return (LoginState.LOGGED_IN if has_cookie else LoginState.LOGGED_OUT), state
        case PageState.LOGGED_OUT:
            return LoginState.LOGGED_OUT, state
        case _:
            await _error_screenshot(page, cfg, state)
            return LoginState.UNKNOWN, state


async def login_state(context: BrowserContext, page: Page, cfg: AppConfig) -> LoginState:
    return (await login_check(context, page, cfg))[0]


async def _error_screenshot(page: Page, cfg: AppConfig, state: PageState) -> None:
    await save_error_screenshot(
        page, project_path(cfg.paths.logs_dir), f"login_{state.value}", cfg.media.jpeg_quality
    )


async def check_login(cfg: AppConfig) -> LoginState:
    async with open_browser(cfg) as context:
        page = await first_page(context)
        return await login_state(context, page, cfg)


def interactive_login(cfg: AppConfig) -> None:
    """Обычный Chrome без Playwright на профиле сервиса: в окне под управлением Playwright
    TikTok вход не пропускает (15.10.2026). Возврат — когда человек закрыл окно."""
    chrome = cfg.browser.chrome_path
    if not chrome.is_file():
        raise BrowserLaunchError(f"Chrome не найден: {chrome} (browser.chrome_path в config.yaml)")
    profile = prepare_profile(project_path(cfg.browser.profile_dir))
    log.info("login_window_opened", chrome=str(chrome))
    subprocess.run(
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--no-first-run",
            *cfg.browser.login_args,
            sel.LOGIN_URL,
        ],
        check=False,
    )
