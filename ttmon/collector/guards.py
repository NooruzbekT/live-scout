import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response

from ttmon.collector import selectors as sel
from ttmon.settings import GuardsConfig
from ttmon.timeutil import now_utc

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class PageState(StrEnum):
    OK = "ok"
    ENDED = "ended"
    FORBIDDEN = "forbidden"
    CAPTCHA = "captcha"
    LOGGED_OUT = "logged_out"
    TIMEOUT = "timeout"


class Action(StrEnum):
    PROCEED = "proceed"
    SKIP = "skip"
    RETRY = "retry"


class StopSlot(Exception):
    def __init__(self, state: PageState, detail: str = "") -> None:
        super().__init__(f"{state}: {detail}" if detail else str(state))
        self.state = state


@dataclass(frozen=True)
class Probe:
    status: int | None
    forbidden_text: bool
    captcha: bool
    login_button: bool


def classify(probe: Probe) -> PageState:
    # капча и 403 важнее «Войти»: на заблокированной странице кнопка может быть, но логин ни при чём
    if probe.captcha:
        return PageState.CAPTCHA
    if probe.status == 403 or probe.forbidden_text:
        return PageState.FORBIDDEN
    if probe.login_button:
        return PageState.LOGGED_OUT
    if probe.status == 404:
        return PageState.ENDED
    return PageState.OK


class StatusTracker:
    """HTTP-статус последнего документа главного фрейма: при клике по карточке
    page.goto не вызывается, и статус иначе не узнать."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.status: int | None = None
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        request = response.request
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self.status = response.status


def _normalize(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е")


async def _has_captcha(page: Page, timeout_ms: float) -> bool:
    if await page.locator(sel.CAPTCHA).count() > 0:
        return True
    dialogs = page.locator(sel.CAPTCHA_DIALOG)
    for i in range(await dialogs.count()):
        try:
            text = await dialogs.nth(i).inner_text(timeout=timeout_ms)
        except PlaywrightError:
            continue  # диалог закрылся между count() и чтением
        if any(marker in text for marker in sel.CAPTCHA_DIALOG_TEXTS):
            return True
    return False


async def probe(page: Page, tracker: StatusTracker, timeout_s: float) -> Probe:
    timeout_ms = timeout_s * 1000
    text = _normalize(await page.locator("body").inner_text(timeout=timeout_ms))
    return Probe(
        status=tracker.status,
        forbidden_text=any(_normalize(m) in text for m in sel.FORBIDDEN_TEXTS),
        captcha=await _has_captcha(page, timeout_ms),
        login_button=await page.locator(sel.LOGIN_BUTTON).filter(visible=True).count() > 0,
    )


class GuardPolicy:
    """Реакция на состояние страницы. «Подряд» = без успешной страницы между
    ними: обнуляет счётчики только OK.

    handle() → PROCEED: страницей пользоваться; SKIP: пропустить эфир / запрос выдачи;
    RETRY: повторить ту же страницу (пауза уже выдержана); StopSlot: остановить слот."""

    def __init__(self, cfg: GuardsConfig, sleep: Sleep = asyncio.sleep) -> None:
        self._cfg = cfg
        self._sleep = sleep
        self.forbidden_in_row = 0
        self.timeouts_in_row = 0

    async def handle(self, state: PageState) -> Action:
        match state:
            case PageState.OK:
                self.forbidden_in_row = 0
                self.timeouts_in_row = 0
                return Action.PROCEED
            case PageState.ENDED:
                return Action.SKIP
            case PageState.CAPTCHA | PageState.LOGGED_OUT:
                raise StopSlot(state)
            case PageState.FORBIDDEN:
                self.forbidden_in_row += 1
                if self.forbidden_in_row >= self._cfg.forbidden_max_in_row:
                    raise StopSlot(state, f"{self.forbidden_in_row} раза подряд")
                log.warning("forbidden_backoff", in_row=self.forbidden_in_row)
                await self._sleep(self._cfg.forbidden_pause_minutes * 60)
                return Action.RETRY
            case PageState.TIMEOUT:
                self.timeouts_in_row += 1
                if self.timeouts_in_row >= self._cfg.timeouts_max_in_row:
                    log.warning("timeouts_backoff", in_row=self.timeouts_in_row)
                    await self._sleep(self._cfg.timeouts_pause_minutes * 60)
                    self.timeouts_in_row = 0
                return Action.SKIP


async def page_state(page: Page, tracker: StatusTracker, timeout_s: float) -> PageState:
    """Страница, которую не удалось даже прочитать, считается таймаутом."""
    try:
        return classify(await probe(page, tracker, timeout_s))
    except PlaywrightError:
        return PageState.TIMEOUT


ERROR_LABEL = re.compile(r"^[a-z_]+$")


async def save_error_screenshot(
    page: Page, logs_dir: Path, label: str, quality: int
) -> Path | None:
    # label попадает в имя файла: только фиксированные метки из кода
    if not ERROR_LABEL.match(label):
        raise ValueError(f"недопустимая метка скриншота: {label!r}")
    errors_dir = logs_dir / "errors"
    errors_dir.mkdir(parents=True, exist_ok=True)
    path = errors_dir / f"{now_utc():%Y%m%dT%H%M%S%fZ}_{label}.jpg"
    try:
        await page.screenshot(path=path, type="jpeg", quality=quality)
    except PlaywrightError as exc:
        log.warning("error_screenshot_failed", label=label, error=str(exc).splitlines()[0])
        return None
    return path
