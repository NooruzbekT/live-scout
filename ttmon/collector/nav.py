from collections.abc import Awaitable, Callable

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_fixed

from ttmon.collector import selectors as sel
from ttmon.settings import GuardsConfig

log = structlog.get_logger(__name__)


def is_network_error(exc: BaseException) -> bool:
    # таймаут не повторяем: это пропуск эфира и счётчик таймаутов в GuardPolicy
    return isinstance(exc, PlaywrightError) and not isinstance(exc, PlaywrightTimeoutError)


async def with_network_retries[T](cfg: GuardsConfig, op: Callable[[], Awaitable[T]]) -> T:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(cfg.network_retries),
        wait=wait_fixed(cfg.network_retry_pause_seconds),
        retry=retry_if_exception(is_network_error),
        reraise=True,
    ):
        with attempt:
            number = attempt.retry_state.attempt_number
            if number > 1:
                log.warning("navigation_retry", attempt=number)
            return await op()
    raise AssertionError("unreachable: AsyncRetrying с reraise=True")


async def dismiss_prompts(page: Page, timeout_s: float) -> None:
    button = page.get_by_role("button", name=sel.DISMISS_PROMPT_RE).filter(visible=True)
    if await button.count() == 0:
        return
    try:
        await button.first.click(timeout=timeout_s * 1000)
    except PlaywrightError:
        log.warning("prompt_dismiss_failed")
        return
    log.info("prompt_dismissed")
