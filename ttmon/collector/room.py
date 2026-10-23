import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ttmon.collector import selectors as sel
from ttmon.collector.guards import Action, GuardPolicy, PageState, Sleep, StatusTracker, page_state
from ttmon.collector.nav import dismiss_prompts
from ttmon.collector.sigi import RoomInfo, SigiError, parse_sigi
from ttmon.settings import AppConfig
from ttmon.timeutil import now_utc

log = structlog.get_logger(__name__)

BackToResults = Callable[[], Awaitable[bool]]
# запись звука эфира в фоне: None — звук не записан
AudioRecorder = Callable[[RoomInfo], Coroutine[Any, Any, Path | None]]

VIDEO_READY_JS = "s => { const v = document.querySelector(s); return !!v && v.readyState >= 2; }"
CHAT_JS = """([msgSel, ownerSel, limit]) => [...document.querySelectorAll(msgSel)].slice(-limit)
  .map(m => ({ author: m.querySelector(ownerSel)?.innerText?.trim() ?? null,
               text: (m.innerText || '').replace(/\\s+/g, ' ').trim() }))"""


@dataclass(frozen=True)
class ChatMessage:
    author: str | None
    text: str


@dataclass(frozen=True)
class RoomCapture:
    info: RoomInfo
    captured_at: datetime
    chat: list[ChatMessage]
    screenshot_jpeg: bytes
    # владелец — вызывающий: дождаться или отменить
    audio: asyncio.Task[Path | None] | None = None


class RoomError(RuntimeError):
    pass


async def read_chat(page: Page, limit: int) -> list[ChatMessage]:
    if limit == 0:
        return []
    raw: list[dict[str, str | None]] = await page.evaluate(
        CHAT_JS, [sel.CHAT_MESSAGE, sel.CHAT_OWNER, limit]
    )
    messages = []
    for item in raw:
        author, text = item.get("author"), item.get("text") or ""
        # innerText сообщения начинается с имени автора
        if author and text.startswith(author):
            text = text[len(author) :].lstrip(" :")
        messages.append(ChatMessage(author=author, text=text))
    return messages


async def read_sigi(page: Page) -> RoomInfo:
    raw = await page.evaluate(
        "id => document.getElementById(id)?.textContent ?? null", sel.SIGI_SCRIPT_ID
    )
    if not raw:
        raise SigiError("на странице нет SIGI_STATE")
    try:
        return parse_sigi(json.loads(raw))
    except ValueError as exc:
        raise SigiError(f"SIGI_STATE не разобран: {exc}") from exc


class RoomVisitor:
    """Открытие эфира кликом по карточке выдачи. StopSlot пробрасывается наверх."""

    def __init__(
        self,
        cfg: AppConfig,
        tracker: StatusTracker,
        policy: GuardPolicy,
        sleep: Sleep,
        rng: random.Random,
        recorder: AudioRecorder | None = None,
    ) -> None:
        self._cfg = cfg
        self._tracker = tracker
        self._policy = policy
        self._sleep = sleep
        self._rng = rng
        self._recorder = recorder

    @property
    def _timeout_ms(self) -> float:
        return self._cfg.guards.page_timeout_seconds * 1000

    async def _state(self, page: Page) -> PageState:
        return await page_state(page, self._tracker, self._cfg.guards.probe_timeout_seconds)

    async def _arrived_state(self, page: Page) -> PageState:
        """Раздел 9.3 п.2: эфир ИЛИ признак ошибки — что появится раньше, без ожидания
        полного таймаута на капче/403."""
        await page.wait_for_url(
            lambda url: "/search/" not in url,
            wait_until="domcontentloaded",
            timeout=self._timeout_ms,
        )
        if self._tracker.status is not None and self._tracker.status >= 400:
            return await self._state(page)
        arrived = (
            page.locator(sel.ROOM_CONTENT)
            .or_(page.locator(sel.CAPTCHA))
            .or_(page.locator(sel.LOGIN_BUTTON).filter(visible=True))
            .or_(page.get_by_text(sel.FORBIDDEN_TEXT_RE))
        )
        await arrived.first.wait_for(state="visible", timeout=self._timeout_ms)
        return await self._state(page)

    async def _enter(self, page: Page, username: str, back_to_results: BackToResults) -> bool:
        while True:
            card = page.locator(sel.search_card_link(username)).first
            await dismiss_prompts(page, self._cfg.guards.probe_timeout_seconds)
            try:
                await card.click(timeout=self._timeout_ms)
            except PlaywrightError as exc:
                raise RoomError(f"карточка @{username} не нажимается") from exc
            try:
                state = await self._arrived_state(page)
            except PlaywrightError:
                state = await self._state(page)
                if state == PageState.OK:
                    state = PageState.TIMEOUT
            action = await self._policy.handle(state)
            if action == Action.PROCEED:
                return True
            if action == Action.SKIP:
                log.info("room_skipped", username=username, state=state)
                return False
            # RETRY: назад на выдачу той же глубины (карточка могла быть в догруженной порции)
            if not await back_to_results():
                raise RoomError(f"не удалось вернуться на выдачу для повтора @{username}")

    async def visit(
        self, page: Page, username: str, seen: set[str], back_to_results: BackToResults
    ) -> RoomCapture | None:
        """None — эфир пропущен (SKIP охраны, закончился, видео не загрузилось)
        или продавец уже снят в этом слоте под другим ником карточки."""
        if not await self._enter(page, username, back_to_results):
            return None
        try:
            await page.wait_for_function(
                VIDEO_READY_JS, arg=sel.ROOM_VIDEO, timeout=self._timeout_ms
            )
        except PlaywrightError:
            await self._policy.handle(PageState.TIMEOUT)  # ради счётчика таймаутов подряд
            log.info("room_skipped", username=username, state="video_not_ready")
            return None

        info = await read_sigi(page)
        if not sel.USERNAME.fullmatch(info.username):
            raise RoomError(f"недопустимый uniqueId в SIGI_STATE: {info.username!r}")
        if info.username != username:
            log.warning("room_username_mismatch", card=username, sigi=info.username)
            # UNIQUE(date, slot, username) — по нику из SIGI, он и пишется в Stream
            if info.username in seen:
                log.info("room_skipped", username=info.username, state="already_captured")
                return None
        if not info.is_live:
            await self._policy.handle(PageState.ENDED)
            log.info("room_skipped", username=username, state="ended", status=info.status)
            return None

        # звук пишется, пока смотрим эфир и ждём первый ответ AI
        audio = (
            asyncio.create_task(self._recorder(info))
            if self._recorder is not None and info.audio_url
            else None
        )
        try:
            await self._sleep(self._rng.uniform(*self._cfg.collect.watch_seconds))
            chat = await read_chat(page, self._cfg.collect.chat_messages)
            screenshot = await page.screenshot(type="jpeg", quality=self._cfg.media.jpeg_quality)
        except BaseException:
            if audio is not None:
                await cancel_audio(audio)
            raise
        return RoomCapture(
            info=info, captured_at=now_utc(), chat=chat, screenshot_jpeg=screenshot, audio=audio
        )


async def cancel_audio(task: asyncio.Task[Path | None]) -> None:
    """Звук не нужен: ffmpeg убит, файл удалён — и недописанный, и готовый
    (запись могла закончиться, пока первый проход AI ждал минутный лимит)."""
    task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(result, Path):
        await asyncio.to_thread(result.unlink, True)
