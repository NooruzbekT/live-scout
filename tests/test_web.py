import asyncio
import datetime as dt
import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Report, Run, RunStatus, Stream
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.settings import AppConfig, load_config
from ttmon.timeutil import today_almaty
from ttmon.web import scheduler as sched
from ttmon.web import service
from ttmon.web.app import create_app
from ttmon.web.jobs import EXIT_START_FAILED, EXIT_TIMEOUT, JobBusy, JobResult, JobRunner
from ttmon.web.pages import job_title
from ttmon.web.service import PatchError, parse_prices_text

CFG = load_config()
PRICES = json.dumps(
    [{"item": "Худи", "amount": 3000, "currency": "KZT", "source": "screen"}], ensure_ascii=False
)


class FakeRunner(JobRunner):
    def __init__(self, busy: bool = False) -> None:
        super().__init__()
        self._fake_busy = busy
        self.started: list[tuple[str, list[str]]] = []

    @property
    def busy(self) -> bool:
        return self._fake_busy

    def start(
        self, name: str, command: Sequence[str], timeout_s: float
    ) -> "asyncio.Task[JobResult]":
        if self._fake_busy:
            raise JobBusy("collect_morning")
        self.started.append((name, list(command)))
        return None  # type: ignore[return-value]


async def _seed(db_file: Path, shots: Path, day: dt.date) -> None:
    shot = shots / day.isoformat() / "morning" / "01_shop1_1007.jpg"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\xff\xd8jpeg")
    engine = create_engine(db_file)
    async with make_sessionmaker(engine)() as session:
        run = Run(
            date=day,
            slot="morning",
            status=RunStatus.PARTIAL,
            started_at=dt.datetime(2026, 10, 21, 5, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 10, 21, 5, 30, tzinfo=dt.UTC),
            rooms_opened=6,
            selling_found=1,
        )
        session.add(run)
        await session.flush()
        common = {
            "run_id": run.id,
            "date": day,
            "slot": "morning",
            "nickname": "Магазин",
            "room_id": "1",
            "title": "",
            "viewers": 1,
            "followers": 1,
            "captured_at": dt.datetime(2026, 10, 21, 5, 7, tzinfo=dt.UTC),
            "chat_json": "[]",
            "source": "search:x",
        }
        session.add(
            Stream(
                **common,
                n=1,
                username="shop1",
                url="https://www.tiktok.com/@shop1/live",
                screenshot_path=str(shot),
                is_selling=True,
                confidence=0.95,
                category="Одежда женская",
                prices_json=PRICES,
            )
        )
        session.add(
            Stream(
                **common,
                n=None,
                username="talk1",
                url="https://www.tiktok.com/@talk1/live",
                screenshot_path=str(shots / "missing.jpg"),
                is_selling=False,
                confidence=0.1,
                category="Другое",
                prices_json="[]",
            )
        )
        await session.commit()
    await engine.dispose()


@pytest.fixture
def day() -> dt.date:
    return today_almaty(CFG.tz)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(tmp_path: Path, day: dt.date, runner: FakeRunner) -> Iterator[TestClient]:
    db_file, shots = tmp_path / "ttmon.db", tmp_path / "screenshots"
    upgrade_to_head(db_file)
    asyncio.run(_seed(db_file, shots, day))
    app = create_app(CFG, db_file, shots, runner=runner, schedule=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body == {"ok": True, "logged_in": True, "running_job": None, "next_runs": {}}


def test_runs_and_streams(client: TestClient, day: dt.date) -> None:
    runs = client.get("/api/runs").json()
    assert [(r["slot"], r["selling_found"]) for r in runs] == [("morning", 1)]

    selling = client.get("/api/streams", params={"selling": True}).json()
    assert [s["username"] for s in selling] == ["shop1"]
    assert selling[0]["prices_text"] == "Худи: 3 000 ₸"
    url = selling[0]["screenshot_url"]
    assert url == f"/media/screenshots/{day.isoformat()}/morning/01_shop1_1007.jpg"
    assert client.get(url).content == b"\xff\xd8jpeg"

    other = client.get("/api/streams", params={"date": "2020-01-01"}).json()
    assert other == []


def _stream_id(client: TestClient, username: str) -> int:
    streams = client.get("/api/streams").json()
    return int(next(s["id"] for s in streams if s["username"] == username))


def test_patch_marks_selling_and_parses_prices(client: TestClient) -> None:
    talk = _stream_id(client, "talk1")

    response = client.patch(
        f"/api/streams/{talk}",
        json={
            "is_selling": True,
            "category": "Обувь",
            "prices_text": "Кроссовки: 12 500; 3000",
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["is_selling"] and body["manual_override"]
    assert body["n"] == 2
    assert body["category"] == "Обувь"
    assert body["prices_text"] == "Кроссовки: 12 500 ₸; 3 000 ₸"


def test_patch_exclude_and_errors(client: TestClient) -> None:
    shop = _stream_id(client, "shop1")
    assert client.patch(f"/api/streams/{shop}", json={"excluded": True}).json()["excluded"]
    bad_category = client.patch(f"/api/streams/{shop}", json={"category": "Космос"})
    assert bad_category.status_code == 422
    bad_price = client.patch(f"/api/streams/{shop}", json={"prices_text": "Худи: дорого"})
    assert bad_price.status_code == 422 and "дорого" in bad_price.json()["detail"]
    assert client.patch("/api/streams/9999", json={"excluded": True}).status_code == 404
    unknown_field = client.patch(f"/api/streams/{shop}", json={"n": 5})
    assert unknown_field.status_code == 422


def test_manual_run_and_report(client: TestClient, runner: FakeRunner, day: dt.date) -> None:
    assert client.post("/api/runs/night", json={}).status_code == 404
    started = client.post("/api/runs/noon", json={})
    assert started.status_code == 202 and started.json() == {"job": "collect_noon"}
    report = client.post(f"/api/reports/{day.isoformat()}", json={})
    assert report.status_code == 202
    assert [cmd[-4:] for _, cmd in runner.started] == [
        ["ttmon", "collect", "--slot", "noon"],
        ["ttmon", "report", "--date", day.isoformat()],
    ]


def test_manual_run_busy(client: TestClient, runner: FakeRunner) -> None:
    runner._fake_busy = True
    response = client.post("/api/runs/morning", json={})
    assert response.status_code == 409
    assert "collect_morning" in response.json()["detail"]


def test_pages(client: TestClient, day: dt.date) -> None:
    index = client.get("/")
    assert index.status_code == 200
    assert all(slot.title in index.text for slot in CFG.slots.values())
    assert "@shop1" in index.text and "@talk1" not in index.text

    page = client.get(f"/day/{day.isoformat()}")
    assert page.status_code == 200
    assert "@shop1" in page.text and "@talk1" in page.text
    filtered = client.get(f"/day/{day.isoformat()}", params={"selling": "false"})
    assert "@talk1" in filtered.text and "@shop1" not in filtered.text


def test_parse_prices_text() -> None:
    prices = parse_prices_text("Пижама 5 шт: 12 500 ₸; ; 8.9к")
    assert [(p.item, p.amount, p.source) for p in prices] == [
        ("Пижама 5 шт", 12500, "manual"),
        (None, 8900, "manual"),
    ]
    assert parse_prices_text("") == []
    with pytest.raises(PatchError):
        parse_prices_text("Худи: дорого")


def test_scheduler_jobs() -> None:
    async def never(_day: dt.date) -> bool:
        return False

    scheduler = sched.build_scheduler(CFG, JobRunner(), never)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    assert set(jobs) == {
        "collect_morning",
        "collect_noon",
        "collect_evening",
        "report_fallback",
        "cleanup",
    }
    assert jobs["collect_evening"].func is sched.evening
    assert jobs["collect_morning"].func is sched.collect_slot
    for slot, slot_cfg in CFG.slots.items():
        fields = {f.name: str(f) for f in jobs[f"collect_{slot}"].trigger.fields}
        assert (fields["hour"], fields["minute"]) == (
            str(slot_cfg.start.hour),
            str(slot_cfg.start.minute),
        )
    settings = {(j.misfire_grace_time, j.coalesce, j.max_instances) for j in jobs.values()}
    assert settings == {(1800, True, 1)}


class RecordingRunner(JobRunner):
    def __init__(self) -> None:
        super().__init__()
        self.ran: list[str] = []

    async def run(self, name: str, command: Sequence[str], timeout_s: float) -> JobResult:
        self.ran.append(" ".join(command[2:]))
        return JobResult(name=name, started_at=dt.datetime.now(dt.UTC))


async def test_evening_builds_report_and_fallback_skips_done() -> None:
    runner = RecordingRunner()
    await sched.evening(CFG, runner, "evening")
    today = today_almaty(CFG.tz).isoformat()
    assert runner.ran == ["ttmon collect --slot evening", f"ttmon report --date {today}"]

    async def done(_day: dt.date) -> bool:
        return True

    await sched.report_fallback(CFG, runner, done)
    assert len(runner.ran) == 2


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


async def test_runner_captures_output_and_exit_code() -> None:
    runner = JobRunner()
    result = await runner.run("echo", _python("print('привет'); raise SystemExit(7)"), 30)
    assert result.exit_code == 7
    assert list(result.tail) == ["привет"]
    assert runner.current is None and runner.last["echo"] is result


async def test_runner_kills_on_timeout() -> None:
    result = await JobRunner().run("hang", _python("import time; time.sleep(60)"), 1)
    assert result.exit_code == EXIT_TIMEOUT


async def test_runner_start_failed() -> None:
    result = await JobRunner().run("broken", ["нет-такой-программы-ttmon"], 5)
    assert result.exit_code == EXIT_START_FAILED


async def test_runner_start_is_exclusive() -> None:
    runner = JobRunner()
    task = runner.start("first", _python("import time; time.sleep(0.5)"), 30)
    with pytest.raises(JobBusy):
        runner.start("second", _python("pass"), 30)
    assert (await task).exit_code == 0
    assert not runner.busy


def test_cleanup(tmp_path: Path) -> None:
    cfg: AppConfig = CFG.model_copy(
        update={
            "paths": CFG.paths.model_copy(
                update={"logs_dir": tmp_path / "logs", "data_dir": tmp_path / "data"}
            )
        }
    )
    today = dt.date(2026, 10, 21)
    logs = tmp_path / "logs"
    (logs / "errors").mkdir(parents=True)
    old_log = logs / "ttmon-2026-09-01-collect.jsonl"
    fresh_log = logs / "ttmon-2026-10-21-serve.jsonl"
    old_error = logs / "errors" / "20260901T100000000000Z_slot_stopped.jpg"
    fresh_error = logs / "errors" / "20261020T100000000000Z_slot_stopped.jpg"
    audio = tmp_path / "data" / "audio" / "2026-10-10"
    audio.mkdir(parents=True)
    old_audio = audio / "shop_2026-10-10.m4a"
    for path in (old_log, fresh_log, old_error, fresh_error, old_audio):
        path.write_text("x", encoding="utf-8")
    # скриншоты ошибок без даты в формате ГГГГ-ММ-ДД — по времени изменения
    old = dt.datetime(2026, 9, 1, 10, tzinfo=dt.UTC).timestamp()
    os.utime(old_error, (old, old))

    removed = sched.cleanup(cfg, today)

    assert removed == 3
    assert fresh_log.exists() and not old_log.exists()
    assert fresh_error.exists() and not old_error.exists()
    assert not audio.exists()


@pytest.mark.parametrize(
    ("hhmm", "already", "expected"),
    [
        ("10:10", False, ["collect_morning"]),
        ("10:10", True, []),
        ("10:40", False, []),
        ("09:50", False, []),
    ],
)
async def test_catch_up_missed_slot(hhmm: str, already: bool, expected: list[str]) -> None:
    async def started(_day: dt.date, _slot: str) -> bool:
        return already

    async def never(_day: dt.date) -> bool:
        return False

    scheduler = sched.build_scheduler(CFG, JobRunner(), never)
    moved: list[tuple[str, dt.datetime]] = []
    scheduler.modify_job = lambda job_id, **kw: moved.append((job_id, kw["next_run_time"]))
    hour, minute = map(int, hhmm.split(":"))
    now = dt.datetime(2026, 10, 21, hour, minute, tzinfo=CFG.tz)

    assert await sched.catch_up(scheduler, CFG, now, started, never) == expected
    assert [job_id for job_id, _ in moved] == expected


@pytest.mark.parametrize(("done", "expected"), [(False, ["report_fallback"]), (True, [])])
async def test_catch_up_report_after_fallback_time(done: bool, expected: list[str]) -> None:
    async def started(_day: dt.date, _slot: str) -> bool:
        return True

    async def report_done(_day: dt.date) -> bool:
        return done

    scheduler = sched.build_scheduler(CFG, JobRunner(), report_done)
    moved: list[str] = []
    scheduler.modify_job = lambda job_id, **_kw: moved.append(job_id)
    now = dt.datetime(2026, 10, 21, 21, 15, tzinfo=CFG.tz)

    assert await sched.catch_up(scheduler, CFG, now, started, report_done) == expected
    assert moved == expected


@pytest.mark.parametrize(
    "headers",
    [
        {},  # форма или fetch no-cors: не JSON
        {"content-type": "application/json", "origin": "https://evil.example"},
        {"content-type": "application/json", "sec-fetch-site": "cross-site"},
    ],
)
def test_cross_site_writes_rejected(
    client: TestClient, runner: FakeRunner, headers: dict[str, str]
) -> None:
    response = client.post("/api/runs/morning", headers=headers)
    assert response.status_code == 403
    assert runner.started == []


def test_same_origin_write_allowed(client: TestClient, runner: FakeRunner) -> None:
    headers = {"origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"}
    assert client.post("/api/runs/morning", json={}, headers=headers).status_code == 202


def test_foreign_host_rejected(client: TestClient) -> None:
    """DNS-rebinding: домен атакующего, перепривязанный на 127.0.0.1."""
    response = client.get("/api/streams", headers={"host": "evil.example:8765"})
    assert response.status_code == 400


def test_day_filter_all_is_empty_string(client: TestClient, day: dt.date) -> None:
    page = client.get(f"/day/{day.isoformat()}", params={"slot": "", "selling": ""})
    assert page.status_code == 200
    assert "@shop1" in page.text and "@talk1" in page.text


def test_index_count_follows_panel_edits(client: TestClient) -> None:
    assert ">1</span>" in client.get("/").text
    shop = _stream_id(client, "shop1")
    client.patch(f"/api/streams/{shop}", json={"excluded": True})
    page = client.get("/").text
    assert ">0</span>" in page and "@shop1" not in page


def test_parse_prices_keeps_currency() -> None:
    prices = parse_prices_text("Кроссовки: 1 200 USD; Крем: 900 руб; Худи: 3000")
    assert [(p.amount, p.currency) for p in prices] == [(1200, "USD"), (900, "RUB"), (3000, "KZT")]


async def test_report_built_after_evening(tmp_path: Path) -> None:
    db_file = tmp_path / "ttmon.db"
    await asyncio.to_thread(upgrade_to_head, db_file)
    day = dt.date(2026, 10, 21)
    engine = create_engine(db_file)
    sessions = make_sessionmaker(engine)

    def at(hour: int) -> dt.datetime:
        return dt.datetime(2026, 10, 21, hour, tzinfo=dt.UTC)

    async with sessions() as session:
        assert not await service.report_built_after_evening(session, day, "evening")
        session.add(Report(date=day, spreadsheet_id="s", url="u", built_at=at(10)))
        await session.commit()
        assert await service.report_built_after_evening(session, day, "evening")
        session.add(
            Run(date=day, slot="evening", status="ok", started_at=at(13), finished_at=at(13))
        )
        await session.commit()
        # отчёт собран вручную днём — вечерних эфиров в нём нет
        assert not await service.report_built_after_evening(session, day, "evening")
    await engine.dispose()


async def test_runner_survives_huge_output_line() -> None:
    code = "import sys; sys.stdout.write('x' * 200000 + chr(10) + 'done' + chr(10))"
    result = await JobRunner().run("long", _python(code), 30)
    assert result.exit_code == 0
    assert list(result.tail)[-1] == "done"
    assert len(list(result.tail)[0]) <= 500


async def test_runner_shutdown_kills_running_job() -> None:
    runner = JobRunner()
    task = runner.start("hang", _python("import time; time.sleep(60)"), 120)
    for _ in range(100):
        if runner._process is not None:
            break
        await asyncio.sleep(0.05)
    await runner.shutdown()
    result = await task
    assert result.exit_code not in (None, 0)
    assert not runner.busy


def test_job_title() -> None:
    assert job_title(CFG, "collect_morning") == "сбор «Утро»"
    assert job_title(CFG, "report_2026-10-21") == "отчёт за 2026-10-21"
