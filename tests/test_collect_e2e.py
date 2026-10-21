"""Сквозной прогон слота против подставного TikTok: настоящий Chrome (headless), страницы
поиска и эфиров отдаются из шаблонов ниже, всё остальное — abort, сеть не используется."""

import contextlib
import json
import random
import re
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.async_api import BrowserContext, Route
from sqlalchemy import select

from ttmon.ai.base import ClassifyInput, ClassifyResult
from ttmon.ai.schema import Classification, Price
from ttmon.collector import selectors as sel
from ttmon.collector.browser import BrowserLaunchError, open_browser
from ttmon.collector.run import CollectResult, SlotCollector
from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Run, RunStatus, Stream
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.settings import AppConfig, load_config

SIGI_FIXTURE = Path(__file__).parent / "fixtures" / "sigi_state_live.json"
HTML = "text/html; charset=utf-8"
LOGGED_IN_HTML = '<html><body><button data-e2e="nav-more-menu">Еще</button></body></html>'
FORBIDDEN_HTML = "<html><body><h1>Доступ к www.tiktok.com запрещен</h1></body></html>"
CAPTCHA_HTML = (
    '<html><body><div id="captcha-verify-container-main-page">Перетащите ползунок</div>'
    "</body></html>"
)
# canvas.captureStream даёт видео с readyState >= 2 без сети
VIDEO_JS = """const c = document.createElement('canvas'); c.width = 64; c.height = 128;
const g = c.getContext('2d'); setInterval(() => { g.fillStyle = '#' + (Math.random()*0xfff|0)
.toString(16).padStart(3, '0'); g.fillRect(0, 0, 64, 128); }, 50);
const v = document.querySelector('video'); v.muted = true; v.srcObject = c.captureStream(10);
v.play();"""
PROMPT_HTML = (
    '<div id="prompt" style="position:fixed;inset:0;z-index:9;background:#0008">'
    "<p>Создайте ключ для быстрого входа</p>"
    "<button onclick=\"document.getElementById('prompt').remove()\">Возможно, позже</button></div>"
)
CHAT_MSG = '<div data-e2e="chat-message"><span data-e2e="message-owner-name">{}</span> {}</div>'


def _card(username: str) -> str:
    return (
        f'<div data-e2e="search_live-item" style="height:1000px">'
        f'<a href="/@{username}/live?enter_from_merge=x">'
        f'<span data-e2e="search-card-user-unique-id">{username}</span></a></div>'
    )


def _room_html(sigi_username: str, status: int) -> str:
    sigi: dict[str, Any] = json.loads(SIGI_FIXTURE.read_text(encoding="utf-8"))
    info = sigi["LiveRoom"]["liveRoomUserInfo"]
    info["user"]["uniqueId"] = sigi_username
    info["user"]["roomId"] = str(7_000_000_000 + sum(map(ord, sigi_username)))
    info["liveRoom"]["status"] = status
    chat = CHAT_MSG.format("aru", "қаншадан?") + CHAT_MSG.format("dana", "бағасы")
    return (
        f'<html><body><div data-e2e="live-room-content"><video></video></div>'
        f'<div data-e2e="live-chat-container">{chat}</div>'
        f'<script id="SIGI_STATE" type="application/json">{json.dumps(sigi, ensure_ascii=False)}'
        f"</script><script>{VIDEO_JS}</script></body></html>"
    )


class FakeTikTok:
    """rooms: username → live | ended | forbidden_once | captcha | nosigi | alias:<ник в SIGI>.
    more — карточки, которые подгружаются только после прокрутки (scroll), как в TikTok."""

    def __init__(
        self, first: list[str], more: list[str], rooms: dict[str, str], prompt: bool = False
    ) -> None:
        self.prompt = prompt
        self.first = first
        self.more = more
        self.rooms = rooms
        self.room_requests: Counter[str] = Counter()
        self.documents: list[str] = []

    def _search_html(self) -> str:
        more = json.dumps("".join(_card(u) for u in self.more))
        return (
            "<html><body>"
            + (PROMPT_HTML if self.prompt else "")
            + '<a href="/live">LIVE</a><div data-e2e="search_live-item-list" id="list">'
            + "".join(_card(u) for u in self.first)
            + f"</div><script>let done = false; window.addEventListener('scroll', () => {{"
            f"if (!done) {{ done = true; document.getElementById('list')"
            f".insertAdjacentHTML('beforeend', {more}); }} }});</script></body></html>"
        )

    def _room(self, user: str) -> tuple[int, str]:
        self.room_requests[user] += 1
        kind = self.rooms.get(user, "live")
        if kind == "forbidden_once" and self.room_requests[user] == 1:
            return 403, FORBIDDEN_HTML
        if kind == "nosigi":
            return 200, _room_html(user, 2).replace('id="SIGI_STATE"', 'id="OTHER"')
        if kind == "captcha":
            return 200, CAPTCHA_HTML
        if kind.startswith("alias:"):
            return 200, _room_html(kind.removeprefix("alias:"), 2)
        return 200, _room_html(user, 4 if kind == "ended" else 2)

    async def handler(self, route: Route) -> None:
        url = route.request.url
        path = urlsplit(url).path
        status, body = 200, ""
        if url.rstrip("/") == sel.LIVE_URL:
            body = LOGGED_IN_HTML
        elif path == "/search/live":
            body = self._search_html()
            self.documents.append("search")
        elif match := re.fullmatch(r"/@([^/]+)/live", path):
            status, body = self._room(match.group(1))
            self.documents.append(f"room:{match.group(1)}")
        else:
            await route.abort()
            return
        await route.fulfill(status=status, body=body, content_type=HTML)

    @contextlib.asynccontextmanager
    async def browser(self, cfg: AppConfig) -> AsyncIterator[BrowserContext]:
        try:
            async with open_browser(cfg) as ctx:
                await ctx.route("**/*", self.handler)
                await ctx.add_cookies(
                    [
                        {
                            "name": sel.SESSION_COOKIE,
                            "value": "x",
                            "domain": ".tiktok.com",
                            "path": "/",
                        }
                    ]
                )
                yield ctx
        except BrowserLaunchError as exc:
            pytest.skip(f"Chrome недоступен: {exc}")


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    base = load_config()
    return base.model_copy(
        update={
            "browser": base.browser.model_copy(
                update={"profile_dir": tmp_path / "profile", "headless": True}
            ),
            "guards": base.guards.model_copy(
                update={
                    "page_timeout_seconds": 5,
                    "login_settle_seconds": 0.5,
                    "network_retries": 1,
                    "network_retry_pause_seconds": 0,
                }
            ),
            "collect": base.collect.model_copy(
                update={
                    "watch_seconds": (0, 0),
                    "pause_between_rooms": (0, 0),
                    "scroll_pause_seconds": (0, 0),
                    "search_queries": ["одежда алматы"],
                    "rotate_queries": False,
                    "search_scrolls": 1,
                }
            ),
            "paths": base.paths.model_copy(update={"logs_dir": tmp_path / "logs"}),
        }
    )


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "ttmon.db"
    upgrade_to_head(path)
    return path


async def _collect(
    config: AppConfig, db_file: Path, tiktok: FakeTikTok, limit: int, sleep: FakeSleep
) -> CollectResult:
    collector = SlotCollector(
        config,
        "test",
        limit,
        db_file.parent / "screenshots",
        sleep,
        random.Random(0),
        tiktok.browser,
    )
    return await collector.collect(db_file)


async def _load(db_file: Path) -> tuple[Run, list[Stream]]:
    engine = create_engine(db_file)
    async with make_sessionmaker(engine)() as session:
        run = (await session.execute(select(Run))).scalar_one()
        streams = list((await session.execute(select(Stream).order_by(Stream.id))).scalars())
    await engine.dispose()
    return run, streams


def _screenshot_ok(stream: Stream) -> bool:
    shot = Path(stream.screenshot_path)
    return (
        shot.is_file() and shot.stat().st_size > 0 and shot.name.startswith(f"{stream.username}_")
    )


async def test_guards_and_limit(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(
        ["shop1", "ended", "blocked403", "shop2", "shop3"],
        [],
        {"ended": "ended", "blocked403": "forbidden_once"},
    )
    sleep = FakeSleep()

    result = await _collect(config, db_file, tiktok, 4, sleep)

    assert (result.status, result.opened, result.saved, result.failed) == (RunStatus.OK, 4, 3, 0)
    assert tiktok.room_requests["blocked403"] == 2
    assert config.guards.forbidden_pause_minutes * 60 in sleep.calls
    # лимит набран — после последнего эфира назад на выдачу не ходим
    assert tiktok.documents[-1] == "room:shop2"

    run, streams = await _load(db_file)
    assert run.rooms_opened == 4 and run.rooms_failed == 0 and run.status == RunStatus.OK
    assert [s.username for s in streams] == ["shop1", "blocked403", "shop2"]
    assert all(s.is_selling is None and s.n is None for s in streams)
    assert streams[0].source == "search:одежда алматы"
    assert streams[0].url == "https://www.tiktok.com/@shop1/live"
    assert json.loads(streams[0].chat_json) == [
        {"author": "aru", "text": "қаншадан?"},
        {"author": "dana", "text": "бағасы"},
    ]
    assert all(_screenshot_ok(s) for s in streams)


async def test_scrolled_cards_alias_and_rerun(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["c1", "alias"], ["s3", "s4"], {"alias": "alias:c1"})

    first = await _collect(config, db_file, tiktok, 10, FakeSleep())

    # карточки из догруженной порции кликаются после возврата; alias — тот же продавец c1
    assert (first.status, first.opened, first.saved, first.failed) == (RunStatus.OK, 4, 3, 0)
    _, streams = await _load(db_file)
    assert [s.username for s in streams] == ["c1", "s3", "s4"]

    again = await _collect(config, db_file, tiktok, 10, FakeSleep())

    # ничего нового, но статус считается по всему Run: эфиры уже сняты
    assert (again.status, again.saved) == (RunStatus.OK, 0)
    run, streams = await _load(db_file)
    assert run.status == RunStatus.OK and len(streams) == 3


async def test_captcha_on_room_stops_slot(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["x1", "cap", "x2"], [], {"cap": "captcha"})

    result = await _collect(config, db_file, tiktok, 10, FakeSleep())

    assert (result.status, result.saved) == (RunStatus.ABORTED, 1)
    assert "room:x2" not in tiktok.documents
    run, _ = await _load(db_file)
    assert run.status == RunStatus.ABORTED
    assert run.error is not None and "captcha" in run.error


class SellingByName:
    """Продающие — ники с префиксом sell; ник bad — ответ так и не прошёл схему."""

    model = "fake"

    async def classify(self, data: ClassifyInput) -> ClassifyResult:
        if data.username == "bad":
            return ClassifyResult(None, "fake", "oops", "ответ не по схеме")
        selling = data.username.startswith("sell")
        verdict = Classification(
            is_selling=selling,
            confidence=0.95 if selling else 0.05,
            category="Одежда женская" if selling else "Другое",
            prices=[Price(item="Куртка", raw="3500тг", amount=3500, source="screen")],
            reason="тест",
        )
        return ClassifyResult(verdict, "fake", "{}")


async def test_ai_numbers_selling_rooms(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["sell1", "game", "bad", "sell2"], [], {})
    collector = SlotCollector(
        config,
        "test",
        10,
        db_file.parent / "screenshots",
        FakeSleep(),
        random.Random(0),
        tiktok.browser,
        SellingByName(),
    )

    result = await collector.collect(db_file)

    assert (result.saved, result.selling) == (4, 2)
    _, streams = await _load(db_file)
    by_name = {s.username: s for s in streams}
    assert (by_name["sell1"].n, by_name["sell2"].n) == (1, 2)
    assert by_name["game"].is_selling is False and by_name["game"].n is None
    assert by_name["bad"].is_selling is None and by_name["bad"].ai_reason == "ответ не по схеме"
    assert Path(by_name["sell2"].screenshot_path).name.startswith("02_sell2_")
    assert Path(by_name["game"].screenshot_path).name.startswith("game_")
    assert json.loads(by_name["sell1"].prices_json or "[]")[0]["amount"] == 3500
    run, _ = await _load(db_file)
    assert run.selling_found == 2
    ai_input = (
        db_file.parent
        / "ai_inputs"
        / Path(by_name["sell1"].screenshot_path).relative_to(db_file.parent / "screenshots")
    )
    assert ai_input.is_file()


async def test_room_error_does_not_kill_slot(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["ok1", "broken", "ok2"], [], {"broken": "nosigi"})

    result = await _collect(config, db_file, tiktok, 10, FakeSleep())

    assert (result.status, result.opened, result.saved, result.failed) == (
        RunStatus.PARTIAL,
        3,
        2,
        1,
    )
    run, streams = await _load(db_file)
    assert (run.rooms_opened, run.rooms_failed) == (3, 1)
    assert [s.username for s in streams] == ["ok1", "ok2"]


async def test_forbidden_retry_on_scrolled_card(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["a1", "a2"], ["deep403"], {"deep403": "forbidden_once"})

    result = await _collect(config, db_file, tiktok, 10, FakeSleep())

    assert (result.saved, result.failed) == (3, 0)
    assert tiktok.room_requests["deep403"] == 2


class FlakyAI:
    """Первые fail_first вызовов — сбой API после всех ретраев (квота/сеть)."""

    model = "flaky"

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.calls = 0

    async def classify(self, data: ClassifyInput) -> ClassifyResult:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("quota or network")
        return await SellingByName().classify(data)


async def test_ai_outage_keeps_room_then_stops_slot(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["sell1", "sell2", "sell3", "sell4", "sell5"], [], {})
    collector = SlotCollector(
        config,
        "test",
        10,
        db_file.parent / "screenshots",
        FakeSleep(),
        random.Random(0),
        tiktok.browser,
        FlakyAI(fail_first=99),
    )

    result = await collector.collect(db_file)

    limit = config.ai.max_consecutive_failures
    assert result.status == RunStatus.FAILED
    run, streams = await _load(db_file)
    # эфиры до порога сохранены с is_selling=None, на пороге слот остановлен
    assert len(streams) == limit - 1
    assert all(s.is_selling is None and s.ai_reason for s in streams)
    assert run.error is not None and "сбоев AI подряд" in run.error


def _with(config: AppConfig, **collect: Any) -> AppConfig:
    return config.model_copy(update={"collect": config.collect.model_copy(update=collect)})


async def test_stops_at_selling_target(config: AppConfig, db_file: Path) -> None:
    cfg = _with(config, target_selling=2, min_selling=2)
    tiktok = FakeTikTok(["sell1", "game", "sell2", "sell3"], [], {})
    collector = SlotCollector(
        cfg,
        "test",
        None,
        db_file.parent / "screenshots",
        FakeSleep(),
        random.Random(0),
        tiktok.browser,
        SellingByName(),
    )

    result = await collector.collect(db_file)

    assert (result.status, result.selling, result.opened) == (RunStatus.OK, 2, 3)
    assert "room:sell3" not in tiktok.documents


async def test_round_robin_ends_when_nothing_new(config: AppConfig, db_file: Path) -> None:
    cfg = _with(config, search_queries=["q1", "q2"])
    tiktok = FakeTikTok(["a", "b"], [], {})

    result = await SlotCollector(
        cfg,
        "test",
        None,
        db_file.parent / "screenshots",
        FakeSleep(),
        random.Random(0),
        tiktok.browser,
    ).collect(db_file)

    # второй запрос и второй круг не дают новых эфиров — выход, а не вечный цикл
    assert (result.status, result.opened, result.saved) == (RunStatus.OK, 2, 2)
    assert tiktok.documents.count("search") >= 2


async def test_passkey_prompt_over_results_dismissed(config: AppConfig, db_file: Path) -> None:
    tiktok = FakeTikTok(["shop1"], ["shop2"], {}, prompt=True)

    result = await _collect(config, db_file, tiktok, 10, FakeSleep())

    assert (result.status, result.saved, result.failed) == (RunStatus.OK, 2, 0)
