import contextlib
import datetime as dt
import random
from dataclasses import dataclass, field

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ttmon.collector import selectors as sel
from ttmon.collector.guards import Action, GuardPolicy, PageState, Sleep, StatusTracker, page_state
from ttmon.collector.nav import dismiss_prompts, with_network_retries
from ttmon.settings import AppConfig

log = structlog.get_logger(__name__)

CARDS_GREW_JS = "([s, before]) => document.querySelectorAll(s).length > before"


@dataclass(frozen=True)
class Candidate:
    username: str
    source: str


@dataclass
class SearchCursor:
    """Позиция в выдаче одного запроса. scrolls нужен, чтобы после go_back докрутить
    до той же глубины: Playwright отключает bfcache, выдача перезагружается с первой порцией."""

    query: str
    scrolls: int = 0
    offered: set[str] = field(default_factory=set)


def query_order(queries: list[str], day: dt.date, slot_index: int, rotate: bool) -> list[str]:
    """Старт со смещения «номер слота + день»: разные слоты и дни
    начинают с разных запросов."""
    if not rotate:
        return list(queries)
    offset = (day.toordinal() + slot_index) % len(queries)
    return queries[offset:] + queries[:offset]


def usernames_from_hrefs(hrefs: list[str | None]) -> list[str]:
    result: list[str] = []
    for href in hrefs:
        match = sel.ROOM_HREF.match(href or "")
        if match and match.group(1) not in result:
            result.append(match.group(1))
    return result


class SearchSource:
    """Поиск эфиров по ключевым словам. Навигацией по выдаче управляет
    цикл слота: open → next_candidate → (эфир) → back_to_results → next_candidate ..."""

    def __init__(self, cfg: AppConfig, sleep: Sleep, rng: random.Random) -> None:
        self._cfg = cfg
        self._sleep = sleep
        self._rng = rng

    @property
    def _timeout_ms(self) -> float:
        return self._cfg.guards.page_timeout_seconds * 1000

    async def open(
        self, page: Page, tracker: StatusTracker, policy: GuardPolicy, cursor: SearchCursor
    ) -> bool:
        while True:
            try:
                await with_network_retries(
                    self._cfg.guards,
                    lambda: page.goto(
                        sel.search_url(cursor.query),
                        wait_until="domcontentloaded",
                        timeout=self._timeout_ms,
                    ),
                )
                await page.locator(sel.SEARCH_CARD).first.wait_for(
                    state="visible", timeout=self._timeout_ms
                )
                state = await self._state(page, tracker)
            except PlaywrightError:
                state = await self._state(page, tracker)
                if state == PageState.OK:
                    state = PageState.TIMEOUT
            action = await policy.handle(state)
            if action == Action.PROCEED:
                await self._restore_depth(page, cursor)
                return True
            if action == Action.SKIP:
                log.warning("search_skipped", query=cursor.query, state=state)
                return False

    async def next_candidate(
        self, page: Page, cursor: SearchCursor, seen: set[str]
    ) -> Candidate | None:
        while True:
            for username in await self._visible_usernames(page):
                if username not in seen and username not in cursor.offered:
                    cursor.offered.add(username)
                    return Candidate(username=username, source=f"search:{cursor.query}")
            if cursor.scrolls >= self._cfg.collect.search_scrolls:
                return None
            await self._scroll(page)
            cursor.scrolls += 1

    async def back_to_results(
        self, page: Page, tracker: StatusTracker, policy: GuardPolicy, cursor: SearchCursor
    ) -> bool:
        try:
            if "/search/live" not in page.url:
                await with_network_retries(
                    self._cfg.guards,
                    lambda: page.go_back(wait_until="domcontentloaded", timeout=self._timeout_ms),
                )
            await page.locator(sel.SEARCH_CARD).first.wait_for(
                state="visible", timeout=self._timeout_ms
            )
            await self._restore_depth(page, cursor)
            return True
        except PlaywrightError:
            log.warning("back_to_search_failed", query=cursor.query)
        return await self.open(page, tracker, policy, cursor)

    async def _restore_depth(self, page: Page, cursor: SearchCursor) -> None:
        for _ in range(cursor.scrolls):
            await self._scroll(page)

    async def _scroll(self, page: Page) -> None:
        """Догрузка выдачи асинхронна: ждём, пока карточек станет больше. Нет роста за таймаут —
        выдача кончилась, это не ошибка."""
        await dismiss_prompts(page, self._cfg.guards.probe_timeout_seconds)
        cards = page.locator(sel.SEARCH_CARD)
        before = await cards.count()
        await page.mouse.wheel(0, self._cfg.collect.scroll_px)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_function(
                CARDS_GREW_JS,
                arg=[sel.SEARCH_CARD, before],
                timeout=self._cfg.collect.scroll_load_timeout_seconds * 1000,
            )
        await self._sleep(self._rng.uniform(*self._cfg.collect.scroll_pause_seconds))

    async def _visible_usernames(self, page: Page) -> list[str]:
        hrefs: list[str | None] = await page.locator(sel.SEARCH_CARD_LINK).evaluate_all(
            "els => els.map(e => e.getAttribute('href'))"
        )
        return usernames_from_hrefs(hrefs)

    async def _state(self, page: Page, tracker: StatusTracker) -> PageState:
        return await page_state(page, tracker, self._cfg.guards.probe_timeout_seconds)
