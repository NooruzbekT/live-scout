import asyncio
import functools
import json
import random
import shutil
import time
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import BrowserContext, Page
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.ai.base import (
    ChatLine,
    Classifier,
    ClassifyInput,
    ClassifyResult,
    Usage,
    VoiceInput,
)
from ttmon.ai.gemini import AIUnavailable
from ttmon.ai.schema import Classification, Price
from ttmon.collector import selectors as sel
from ttmon.collector.browser import LoginState, first_page, login_check, open_browser
from ttmon.collector.guards import (
    GuardPolicy,
    PageState,
    Sleep,
    StatusTracker,
    StopSlot,
    save_error_screenshot,
)
from ttmon.collector.room import (
    AudioRecorder,
    BackToResults,
    RoomCapture,
    RoomVisitor,
    cancel_audio,
)
from ttmon.collector.sigi import RoomInfo
from ttmon.collector.sources import Candidate, SearchCursor, SearchSource, query_order
from ttmon.db import repo
from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Run, RunStatus, Stream
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.lock import exclusive_lock
from ttmon.logging import redact
from ttmon.media.audio import record_audio
from ttmon.media.layout import AUDIO_DIR, SCREENSHOTS_DIR, ai_input_path
from ttmon.media.resize import fit_width
from ttmon.media.stamp import stamp_image, stamp_text
from ttmon.settings import PROJECT_ROOT, AppConfig, project_path
from ttmon.timeutil import now_utc, to_almaty, today_almaty

log = structlog.get_logger(__name__)

BrowserFactory = Callable[[AppConfig], AbstractAsyncContextManager[BrowserContext]]
# record_audio(url, out, seconds): подменяется в тестах, ffmpeg и сеть не нужны
RecordAudio = Callable[[str, Path, int], Coroutine[Any, Any, Path | None]]
TEST_SLOT = "test"


@dataclass
class CollectResult:
    run_id: int
    status: RunStatus
    opened: int
    saved: int
    failed: int
    selling: int = 0


def _error_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}".splitlines()[0])


def screenshot_path(
    screenshots_dir: Path, capture: RoomCapture, slot: str, cfg: AppConfig, n: int | None
) -> Path:
    local = to_almaty(capture.captured_at, cfg.tz)
    prefix = f"{n:02d}_" if n is not None else ""
    name = f"{prefix}{capture.info.username}_{local:%H%M}.jpg"
    path = screenshots_dir / local.date().isoformat() / slot / name
    if not path.resolve().is_relative_to(screenshots_dir.resolve()):
        raise ValueError(f"путь скриншота вне {screenshots_dir}: {path}")
    return path


def _stored_path(path: Path) -> str:
    return (
        path.relative_to(PROJECT_ROOT).as_posix()
        if path.is_relative_to(PROJECT_ROOT)
        else str(path)
    )


def write_screenshot(
    capture: RoomCapture, slot: str, screenshots_dir: Path, cfg: AppConfig, n: int | None
) -> Path:
    local = to_almaty(capture.captured_at, cfg.tz)
    jpeg = stamp_image(
        capture.screenshot_jpeg,
        stamp_text(local, cfg.media.stamp_city, capture.info.username),
        project_path(cfg.media.font_path),
        cfg.media.jpeg_quality,
    )
    path = screenshot_path(screenshots_dir, capture, slot, cfg, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg)
    return path


def audio_path(audio_dir: Path, info: RoomInfo, slot: str, cfg: AppConfig) -> Path:
    local = to_almaty(now_utc(), cfg.tz)
    path = audio_dir / local.date().isoformat() / slot / f"{info.username}_{local:%H%M}.mp3"
    if not path.resolve().is_relative_to(audio_dir.resolve()):
        raise ValueError(f"путь звука вне {audio_dir}: {path}")
    return path


def needs_voice(send_audio: str, selling: bool | None, c: Classification | None) -> bool:
    """Второй проход со звуком — только для продающего; auto — если цен не нашлось."""
    if not selling or c is None or send_audio == "never":
        return False
    return send_audio == "always" or not c.prices


def merge_voice_prices(c: Classification, voice: list[Price]) -> int:
    """Сумма с экрана голосом не дублируется (товар там и в речи называют по-разному);
    внутри голоса — по товару и сумме: «платье 5000, юбка 5000» — две цены, одна цена,
    сказанная на двух языках, — одна. Возвращает число добавленных."""
    screen = {(p.amount, p.currency) for p in c.prices}
    heard: set[tuple[str | None, int, str]] = set()
    added = []
    for price in voice:
        key = ((price.item or "").casefold() or None, price.amount, price.currency)
        if (price.amount, price.currency) in screen or key in heard:
            continue
        heard.add(key)
        added.append(price)
    c.prices = [*c.prices, *added]
    return len(added)


def is_selling(result: ClassifyResult | None, threshold: float) -> bool | None:
    """None — AI не спрашивали (--no-ai) или ответ так и не прошёл схему: в отчёт не идёт."""
    if result is None or result.classification is None:
        return None
    c = result.classification
    return c.is_selling and c.confidence >= threshold


def write_ai_input(image: bytes, screenshot: Path) -> Path:
    path = ai_input_path(screenshot)
    if path is None:
        raise ValueError(f"скриншот вне папки {SCREENSHOTS_DIR}: {screenshot}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return path


def build_stream(
    capture: RoomCapture,
    candidate: Candidate,
    run: Run,
    screenshot: Path,
    ai: ClassifyResult | None,
    selling: bool | None,
    n: int | None,
    audio: Path | None = None,
) -> Stream:
    info = capture.info
    c = ai.classification if ai else None
    return Stream(
        run_id=run.id,
        date=run.date,
        slot=run.slot,
        username=info.username,
        nickname=info.nickname,
        room_id=info.room_id,
        title=info.title,
        viewers=info.viewers,
        followers=info.followers,
        live_started_at=info.live_started_at,
        captured_at=capture.captured_at,
        url=sel.room_url(info.username),
        source=candidate.source,
        screenshot_path=_stored_path(screenshot),
        chat_json=json.dumps([asdict(m) for m in capture.chat], ensure_ascii=False),
        n=n,
        is_selling=selling,
        confidence=c.confidence if c else None,
        category=c.category if c else None,
        products_json=json.dumps(c.products, ensure_ascii=False) if c else None,
        prices_json=(
            json.dumps([p.model_dump() for p in c.prices], ensure_ascii=False) if c else None
        ),
        ai_reason=c.reason if c else (ai.error if ai else None),
        ai_model=ai.model if ai else None,
        ai_raw=ai.raw if ai else None,
        audio_path=_stored_path(audio) if audio else None,
    )


def final_status(
    saved_total: int, failed_total: int, selling_total: int | None, min_selling: int
) -> RunStatus:
    """По всему Run, а не по текущему процессу: перезапуск продолжает слот.
    С AI — по норме min_selling продающих, без AI — по наличию эфиров."""
    if selling_total is not None:
        if selling_total >= min_selling:
            return RunStatus.OK
        return RunStatus.PARTIAL if selling_total > 0 else RunStatus.FAILED
    if saved_total == 0:
        return RunStatus.FAILED
    return RunStatus.OK if failed_total == 0 else RunStatus.PARTIAL


class SlotCollector:
    def __init__(
        self,
        cfg: AppConfig,
        slot: str,
        limit: int | None,
        screenshots_dir: Path,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        browser: BrowserFactory = open_browser,
        classifier: Classifier | None = None,
        record: RecordAudio | None = None,
    ) -> None:
        self._cfg = cfg
        self._slot = slot
        self._limit = limit
        self._screenshots_dir = screenshots_dir
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._browser = browser
        self._classifier = classifier
        self._record = record if classifier is not None else None
        self._audio_dir = screenshots_dir.parent / AUDIO_DIR
        self.selling = 0
        self.ai_tokens = Usage()
        self._ai_failures_in_row = 0
        self.opened = 0
        self.saved = 0
        self.failed = 0
        self._started = 0.0

    def _limits_reached(self, run: Run) -> bool:
        collect = self._cfg.collect
        return (
            (self._limit is not None and self.opened >= self._limit)
            or (self._classifier is not None and run.selling_found >= collect.target_selling)
            or run.rooms_opened >= collect.max_rooms_opened
            or time.monotonic() - self._started >= collect.max_minutes * 60
        )

    async def _error_screenshot(self, page: Page, label: str) -> None:
        await save_error_screenshot(
            page, project_path(self._cfg.paths.logs_dir), label, self._cfg.media.jpeg_quality
        )

    async def _room(
        self,
        session: AsyncSession,
        run: Run,
        page: Page,
        visitor: RoomVisitor,
        candidate: Candidate,
        seen: set[str],
        back_to_results: BackToResults,
    ) -> None:
        seen.add(candidate.username)
        self.opened += 1
        written: list[Path] = []
        capture: RoomCapture | None = None
        audio: Path | None = None
        try:
            capture = await visitor.visit(page, candidate.username, seen, back_to_results)
            if capture is None:
                run.rooms_opened += 1
                await session.commit()
                return
            ai_image = fit_width(
                capture.screenshot_jpeg, self._cfg.ai.max_image_width, self._cfg.media.jpeg_quality
            )
            ai = await self._classify(capture, ai_image)
            selling = is_selling(ai, self._cfg.ai.selling_threshold)
            audio = await self._voice(capture, ai, selling)
            if audio is not None:
                written.append(audio)
            n = await repo.next_n(session, run.id) if selling else None
            screenshot = write_screenshot(capture, run.slot, self._screenshots_dir, self._cfg, n)
            written.append(screenshot)
            written.append(write_ai_input(ai_image, screenshot))
            session.add(build_stream(capture, candidate, run, screenshot, ai, selling, n, audio))
            run.rooms_opened += 1
            run.selling_found += bool(selling)
            await session.commit()
            seen.add(capture.info.username)
            self.saved += 1
            self.selling += bool(selling)
            log.info(
                "room_saved",
                username=capture.info.username,
                source=candidate.source,
                selling=selling,
                n=n,
            )
        except (StopSlot, AIUnavailable):
            raise
        except Exception as exc:
            # ошибка одного эфира не роняет слот: Stream не пишем
            await session.rollback()
            # rollback экспайрит run: без refresh следующее чтение атрибута — ленивый IO
            # вне greenlet (MissingGreenlet) и падение всего слота
            await session.refresh(run)
            for path in written:
                path.unlink(missing_ok=True)
            self.failed += 1
            run.rooms_opened += 1
            run.rooms_failed += 1
            log.warning("room_failed", username=candidate.username, error=_error_text(exc))
            await self._error_screenshot(page, "room_error")
            await session.commit()
        finally:
            # звук, не дошедший до Stream: запись могла закончиться до сбоя эфира
            if capture is not None and capture.audio is not None and audio is None:
                await cancel_audio(capture.audio)

    def _recorder(self, run: Run) -> AudioRecorder | None:
        record = self._record
        if record is None:
            return None
        seconds = self._cfg.collect.audio_seconds

        def start(info: RoomInfo) -> Coroutine[Any, Any, Path | None]:
            assert info.audio_url is not None
            out = audio_path(self._audio_dir, info, run.slot, self._cfg)
            return record(info.audio_url, out, seconds)

        return start

    async def _voice(
        self, capture: RoomCapture, ai: ClassifyResult | None, selling: bool | None
    ) -> Path | None:
        """Второй проход со звуком (раздел 10). Путь — если звук ушёл в AI и сохранён.
        Звук не записан или AI упал — эфир остаётся с ценами первого прохода."""
        task = capture.audio
        if task is None:
            return None
        c = ai.classification if ai else None
        if self._classifier is None or not needs_voice(self._cfg.ai.send_audio, selling, c):
            await cancel_audio(task)
            return None
        assert c is not None
        info = capture.info
        try:
            audio = await task
        except Exception as exc:
            log.warning("audio_failed", username=info.username, error=_error_text(exc))
            return None
        if audio is None:
            return None
        try:
            voice = await self._classifier.listen(
                VoiceInput(
                    audio_mp3=await asyncio.to_thread(audio.read_bytes),
                    username=info.username,
                    nickname=info.nickname,
                    title=info.title,
                    products=c.products,
                )
            )
        except Exception as exc:
            # любой сбой (и AIUnavailable) не роняет эфир и не идёт в счётчик сбоев AI:
            # кончилась квота — слот остановит первый проход следующего эфира
            log.warning("ai_voice_failed", username=info.username, error=_error_text(exc))
            await asyncio.to_thread(audio.unlink, True)
            return None
        self._add_usage(voice.usage)
        if voice.error is not None:
            log.warning("ai_voice_failed", username=info.username, error=voice.error)
        added = merge_voice_prices(c, voice.prices)
        log.info("voice_prices", username=info.username, found=len(voice.prices), added=added)
        return audio

    def _add_usage(self, usage: Usage) -> None:
        """Фактический расход токенов за слот (оба прохода) — в лог slot_finished."""
        self.ai_tokens = Usage(
            self.ai_tokens.prompt_tokens + usage.prompt_tokens,
            self.ai_tokens.output_tokens + usage.output_tokens,
            self.ai_tokens.thinking_tokens + usage.thinking_tokens,
        )

    async def _classify(self, capture: RoomCapture, image: bytes) -> ClassifyResult | None:
        """Сбой API после всех ретраев — эфир пишется с is_selling=None;
        ai.max_consecutive_failures таких подряд — AI недоступен (квота), стоп слота."""
        if self._classifier is None:
            return None
        info = capture.info
        try:
            result = await self._classifier.classify(
                ClassifyInput(
                    image_jpeg=image,
                    username=info.username,
                    nickname=info.nickname,
                    title=info.title,
                    viewers=info.viewers,
                    chat=[ChatLine(m.author, m.text) for m in capture.chat],
                )
            )
        except AIUnavailable:
            raise
        except Exception as exc:
            self._ai_failures_in_row += 1
            error = _error_text(exc)
            log.warning("ai_failed", username=info.username, error=error)
            if self._ai_failures_in_row >= self._cfg.ai.max_consecutive_failures:
                raise AIUnavailable(f"{self._ai_failures_in_row} сбоев AI подряд: {error}") from exc
            return ClassifyResult(None, self._classifier.model, None, error)
        self._ai_failures_in_row = 0
        self._add_usage(result.usage)
        return result

    async def _loop(self, session: AsyncSession, run: Run, page: Page) -> None:
        cfg = self._cfg
        seen = await repo.seen_usernames(session, run.id)
        tracker = StatusTracker(page)
        policy = GuardPolicy(cfg.guards, self._sleep)
        visitor = RoomVisitor(
            cfg, tracker, policy, self._sleep, self._rng, recorder=self._recorder(run)
        )
        source = SearchSource(cfg, self._sleep, self._rng)
        slots: list[str] = list(cfg.slots)
        slot_index = slots.index(self._slot) if self._slot in slots else 0
        queries = query_order(
            cfg.collect.search_queries, run.date, slot_index, cfg.collect.rotate_queries
        )

        # запросы по кругу: за время слота в выдаче появляются новые эфиры;
        # полный проход без единого нового эфира — источники исчерпаны
        while not self._limits_reached(run):
            opened_before = self.opened
            for query in queries:
                if self._limits_reached(run):
                    return
                await self._query(session, run, page, query, seen, tracker, policy, visitor, source)
            if self.opened == opened_before:
                log.info("sources_exhausted", opened=self.opened)
                return

    async def _query(
        self,
        session: AsyncSession,
        run: Run,
        page: Page,
        query: str,
        seen: set[str],
        tracker: StatusTracker,
        policy: GuardPolicy,
        visitor: RoomVisitor,
        source: SearchSource,
    ) -> None:
        cursor = SearchCursor(query)
        if not await source.open(page, tracker, policy, cursor):
            return
        while not self._limits_reached(run):
            candidate = await source.next_candidate(page, cursor, seen)
            if candidate is None:
                return
            await self._room(
                session,
                run,
                page,
                visitor,
                candidate,
                seen,
                functools.partial(source.back_to_results, page, tracker, policy, cursor),
            )
            # лимит до навигации: лишний go_back может словить 403/капчу у готового слота
            if self._limits_reached(run):
                return
            if not await source.back_to_results(page, tracker, policy, cursor):
                return
            await self._sleep(self._rng.uniform(*self._cfg.collect.pause_between_rooms))

    async def _finish(
        self, session: AsyncSession, run: Run, status: RunStatus, error: str | None
    ) -> None:
        for attempt in range(2):
            run.status, run.finished_at, run.error = status, now_utc(), error
            try:
                await session.commit()
                return
            except Exception:
                # сессия после упавшего commit требует rollback, иначе Run останется running
                await session.rollback()
                if attempt:
                    raise

    async def collect(self, db_file: Path) -> CollectResult:
        engine = create_engine(db_file)
        try:
            async with make_sessionmaker(engine)() as session:
                run = await repo.get_or_create_run(
                    session, today_almaty(self._cfg.tz), self._slot, now_utc()
                )
                structlog.contextvars.bind_contextvars(run_id=run.id, slot=self._slot)
                log.info("slot_started", limit=self._limit)
                self._started = time.monotonic()
                status = RunStatus.FAILED
                error: str | None = None
                try:
                    async with self._browser(self._cfg) as context:
                        page = await first_page(context)
                        login, page_state = await login_check(context, page, self._cfg)
                        if login != LoginState.LOGGED_IN:
                            reason = (
                                PageState.LOGGED_OUT
                                if login == LoginState.LOGGED_OUT
                                else page_state
                            )
                            raise StopSlot(reason, f"проверка входа: {login}")
                        try:
                            await self._loop(session, run, page)
                        except StopSlot:
                            await self._error_screenshot(page, "slot_stopped")
                            raise
                    status = final_status(
                        await repo.count_streams(session, run.id),
                        run.rooms_failed,
                        run.selling_found if self._classifier is not None else None,
                        self._cfg.collect.min_selling,
                    )
                except StopSlot as exc:
                    status = RunStatus.ABORTED
                    error = redact(str(exc))
                    log.error("slot_stopped", reason=str(exc))
                except AIUnavailable as exc:
                    status = RunStatus.FAILED
                    error = _error_text(exc)
                    log.error("slot_ai_unavailable", error=error)
                except Exception as exc:
                    status = RunStatus.FAILED
                    error = _error_text(exc)
                    log.exception("slot_failed")
                    raise
                finally:
                    await self._finish(session, run, status, error)
                    log.info(
                        "slot_finished",
                        status=status,
                        opened=self.opened,
                        saved=self.saved,
                        failed=self.failed,
                        selling=self.selling,
                        ai_prompt_tokens=self.ai_tokens.prompt_tokens,
                        ai_output_tokens=self.ai_tokens.output_tokens,
                        ai_thinking_tokens=self.ai_tokens.thinking_tokens,
                    )
                return CollectResult(
                    run.id, status, self.opened, self.saved, self.failed, self.selling
                )
        finally:
            await engine.dispose()


def collect_lock_path(cfg: AppConfig) -> Path:
    # одна блокировка на сбор (боевой и test) и login: профиль Chrome у них общий
    return project_path(cfg.paths.data_dir) / "collect.lock"


def _slot_storage(cfg: AppConfig, slot: str) -> tuple[Path, Path]:
    """(db_file, screenshots_dir). Слот test живёт в отдельной папке и начинает с нуля."""
    data_dir = project_path(cfg.paths.data_dir).resolve()
    if slot != TEST_SLOT:
        return project_path(cfg.paths.db_file), data_dir / SCREENSHOTS_DIR
    test_dir = project_path(cfg.paths.test_data_dir).resolve()
    # rmtree только внутри data_dir, не сам data_dir и не корень проекта (secrets/, chrome-profile/)
    if (
        PROJECT_ROOT.resolve().is_relative_to(data_dir)
        or test_dir == data_dir
        or not test_dir.is_relative_to(data_dir)
    ):
        raise ValueError(
            f"paths.test_data_dir должен быть вложен в paths.data_dir внутри проекта: {test_dir}"
        )
    if test_dir.exists():
        shutil.rmtree(test_dir)
    return test_dir / "ttmon.db", test_dir / SCREENSHOTS_DIR


def collect_command(
    cfg: AppConfig, slot: str, limit: int | None, classifier: Classifier | None
) -> CollectResult:
    with exclusive_lock(collect_lock_path(cfg)):
        db_file, screenshots_dir = _slot_storage(cfg, slot)
        upgrade_to_head(db_file)
        record = record_audio if cfg.ai.send_audio != "never" else None
        collector = SlotCollector(
            cfg, slot, limit, screenshots_dir, classifier=classifier, record=record
        )
        return asyncio.run(collector.collect(db_file))
