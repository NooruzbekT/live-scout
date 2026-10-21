import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tests.google_fakes import FakeDriveService, FakeSheet, FakeSheetsService
from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Report, Run, RunStatus, Stream
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.google.drive import SHEET_MIME, Drive
from ttmon.google.sheets import Link, Sheets, SheetsError
from ttmon.report.build import (
    NO_SCREENSHOT,
    TOTAL_LABEL,
    Target,
    build_report,
    prices_text,
    sheet_rows,
)
from ttmon.settings import load_config

CFG = load_config()
type Fields = dict[str, Any]
DAY = dt.date(2026, 10, 21)
PRICES = json.dumps(
    [{"item": "Пижама 5 шт", "amount": 12500, "currency": "KZT", "source": "screen"}],
    ensure_ascii=False,
)


def _stream(
    tmp_path: Path, run: Run, username: str, n: int | None, *, hhmm: str = "1007", **fields: Any
) -> Stream:
    shot = tmp_path / "shots" / run.slot / f"{n or 0:02d}_{username}_{hhmm}.jpg"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"\xff\xd8jpeg")
    hour, minute = int(hhmm[:2]), int(hhmm[2:])
    values: dict[str, object] = {
        "run_id": run.id,
        "date": run.date,
        "slot": run.slot,
        "n": n,
        "username": username,
        "nickname": username,
        "room_id": "1",
        "title": "",
        "viewers": 10,
        "followers": 10,
        "captured_at": dt.datetime(2026, 10, 21, hour - 5, minute, tzinfo=dt.UTC),  # Алматы UTC+5
        "url": f"https://www.tiktok.com/@{username}/live",
        "source": "search:одежда алматы",
        "screenshot_path": str(shot),
        "chat_json": "[]",
        "is_selling": True,
        "category": "Одежда женская",
        "prices_json": PRICES,
    }
    values.update(fields)
    return Stream(**values)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = tmp_path / "ttmon.db"
        upgrade_to_head(self.db)
        self.drive_service = FakeDriveService()
        self.sheets_service = FakeSheetsService(self.drive_service)

    async def seed(self, slot: str, streams: list[tuple[str, int | None, Fields]]) -> None:
        engine = create_engine(self.db)
        async with make_sessionmaker(engine)() as session:
            run = Run(date=DAY, slot=slot, status=RunStatus.OK, started_at=dt.datetime.now(dt.UTC))
            session.add(run)
            await session.flush()
            for username, n, fields in streams:
                session.add(_stream(self.tmp_path, run, username, n, **fields))
            await session.commit()
        await engine.dispose()

    async def build(self, *, test: bool = False, template_id: str = "") -> tuple[str, list[Stream]]:
        engine = create_engine(self.db)
        async with make_sessionmaker(engine)() as session:
            result = await build_report(
                session,
                Drive(self.drive_service),
                Sheets(self.sheets_service),
                CFG,
                DAY,
                Target(test=test),
                template_id,
            )
            streams = list((await session.execute(select(Stream).order_by(Stream.id))).scalars())
            report = (await session.execute(select(Report))).scalar_one()
            assert report.url == result.url
        await engine.dispose()
        return report.spreadsheet_id, streams

    def sheet(self, spreadsheet_id: str, title: str) -> FakeSheet:
        return next(s for s in self.drive_service.spreadsheets[spreadsheet_id] if s.title == title)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_sheet_rows_numbering_and_total() -> None:
    streams = [
        Stream(
            date=DAY,
            captured_at=dt.datetime(2026, 10, 21, 5, 7, tzinfo=dt.UTC),
            category="Обувь",
            prices_json=PRICES,
            drive_url="https://drive.google.com/file/d/a/view",
        ),
        Stream(
            date=DAY,
            captured_at=dt.datetime(2026, 10, 21, 5, 31, tzinfo=dt.UTC),
            category=None,
            prices_json="[]",
            drive_url=None,
        ),
    ]

    rows = sheet_rows(streams, CFG.tz, "не указана")

    assert rows == [
        [
            "21.10.2026",
            1,
            "Обувь",
            Link("Скриншот 10:07", "https://drive.google.com/file/d/a/view"),
            "Пижама 5 шт: 12\u00a0500\u00a0₸",
        ],
        ["21.10.2026", 2, "", NO_SCREENSHOT, "не указана"],
        [TOTAL_LABEL, 2],
    ]


def test_prices_text_without_ai() -> None:
    assert prices_text(None, "не указана") == "не указана"


async def test_report_uploads_selling_and_fills_sheets(env: Env) -> None:
    await env.seed(
        "morning",
        [
            ("shop2", 2, {}),
            ("shop1", 1, {}),
            ("talk", None, {"is_selling": False}),
            ("hidden", 3, {"excluded": True}),
        ],
    )
    await env.seed("evening", [("eve", 1, {"hhmm": "1812"})])

    spreadsheet_id, streams = await env.build()

    by_name = {s.username: s for s in streams}
    assert by_name["shop1"].drive_url and by_name["eve"].drive_url
    assert by_name["talk"].drive_file_id is None and by_name["hidden"].drive_file_id is None
    assert env.drive_service.uploads == 3
    morning = env.sheet(spreadsheet_id, "Утро")
    assert morning.row(0)[0] == "Дата"
    assert [morning.row(r)[:3] for r in (1, 2)] == [
        ["21.10.2026", 1, "Одежда женская"],
        ["21.10.2026", 2, "Одежда женская"],
    ]
    assert morning.row(1)[3][0] == "Скриншот 10:07"
    assert morning.row(3) == [TOTAL_LABEL, 2]
    assert env.sheet(spreadsheet_id, "Обед").row(1) == [TOTAL_LABEL, 0]
    assert env.sheet(spreadsheet_id, "Вечер").row(1)[3][0] == "Скриншот 18:12"
    day_folder = env.drive_service.by_name("2026-10-21")[0]
    assert env.drive_service.files_by_id[spreadsheet_id].parents == [day_folder]
    assert env.drive_service.files_by_id[spreadsheet_id].name == "Отчёт TT эфиры 21.10.2026"


async def test_rebuild_same_file_no_reupload(env: Env) -> None:
    await env.seed("morning", [("shop1", 1, {}), ("shop2", 2, {})])
    first_id, _ = await env.build()

    engine = create_engine(env.db)
    async with make_sessionmaker(engine)() as session:
        stream = (
            await session.execute(select(Stream).where(Stream.username == "shop1"))
        ).scalar_one()
        stream.excluded = True
        await session.commit()
    await engine.dispose()

    second_id, _ = await env.build()

    assert second_id == first_id
    assert env.drive_service.uploads == 2
    assert len(env.drive_service.by_name("Отчёт TT эфиры 21.10.2026", SHEET_MIME)) == 1
    morning = env.sheet(first_id, "Утро")
    assert morning.row(1)[:2] == ["21.10.2026", 1]
    assert morning.row(2) == [TOTAL_LABEL, 1]
    assert morning.row(3) == []


async def test_report_copies_template(env: Env) -> None:
    template_id = Drive(env.drive_service).create_sheet("Шаблон", "root")
    env.drive_service.spreadsheets[template_id] = [
        FakeSheet(1, "Утро", cells={(0, 0): "Дата", (1, 0): "ДД.ММ.ГГГГ", (2, 0): "Итого:"}),
        FakeSheet(2, "Обед"),
        FakeSheet(3, "Вечер"),
    ]
    await env.seed("morning", [("shop1", 1, {})])

    spreadsheet_id, _ = await env.build(template_id=template_id)

    assert spreadsheet_id != template_id
    morning = env.sheet(spreadsheet_id, "Утро")
    assert morning.row(0) == ["Дата"]
    assert morning.row(1)[:2] == ["21.10.2026", 1]
    assert morning.row(2) == [TOTAL_LABEL, 1]
    assert env.sheet(template_id, "Утро").row(1) == ["ДД.ММ.ГГГГ"]


async def test_test_report_separate_folder_first_sheet(env: Env) -> None:
    await env.seed("test", [("shop1", 1, {})])

    spreadsheet_id, streams = await env.build(test=True)

    assert env.drive_service.by_name("TT эфиры (тест)")
    assert not env.drive_service.by_name("TT эфиры")
    assert env.drive_service.by_name("Тест")
    assert env.drive_service.files_by_id[spreadsheet_id].name.startswith("ТЕСТ ")
    assert env.sheet(spreadsheet_id, "Утро").row(2) == [TOTAL_LABEL, 1]
    assert streams[0].drive_url is not None


async def test_missing_screenshot_does_not_stop_report(env: Env, tmp_path: Path) -> None:
    await env.seed("morning", [("gone", 1, {}), ("shop", 2, {})])
    next((tmp_path / "shots" / "morning").glob("01_gone_*.jpg")).unlink()

    spreadsheet_id, streams = await env.build()

    assert env.drive_service.uploads == 1
    assert env.sheet(spreadsheet_id, "Утро").row(1)[3] == NO_SCREENSHOT
    assert {s.username: s.drive_url is not None for s in streams} == {"gone": False, "shop": True}


async def test_failed_build_retried_without_duplicate_copy(env: Env) -> None:
    template_id = Drive(env.drive_service).create_sheet("Шаблон", "root")
    env.drive_service.spreadsheets[template_id] = [FakeSheet(1, "Утро"), FakeSheet(2, "Обед")]
    await env.seed("morning", [("shop1", 1, {})])

    for _ in range(2):
        with pytest.raises(SheetsError, match="Вечер"):
            await env.build(template_id=template_id)

    assert len(env.drive_service.by_name("Отчёт TT эфиры 21.10.2026", SHEET_MIME)) == 1


@pytest.fixture
def fresh(tmp_path: Path) -> Env:
    """Новая БД (collect --slot test её пересоздаёт), Drive тот же — подставляется в тесте."""
    return Env(tmp_path / "again")


async def test_test_report_found_by_name_after_db_recreated(env: Env, fresh: Env) -> None:
    await env.seed("test", [("shop1", 1, {})])
    first_id, _ = await env.build(test=True)

    fresh.drive_service, fresh.sheets_service = env.drive_service, env.sheets_service
    await fresh.seed("test", [("shop2", 1, {})])
    second_id, _ = await fresh.build(test=True)

    assert second_id == first_id
    assert len(env.drive_service.by_name("ТЕСТ Отчёт TT эфиры 21.10.2026", SHEET_MIME)) == 1


def _template_tab(sheet_id: int, title: str) -> FakeSheet:
    """Как xlsx-шаблон: заголовок, 3 строки-примера и «Итого», все строки в рамках."""
    cells = {(0, 0): "Дата", (1, 0): "ДД.ММ.ГГГГ", (4, 0): "Итого:"}
    formats = {0: "заголовок", **dict.fromkeys(range(1, 5), "строка")}
    return FakeSheet(sheet_id, title, row_count=1000, cells=cells, formats=formats)


@pytest.mark.parametrize("count", [1, 6])
async def test_rows_get_template_format_and_leftovers_cleared(env: Env, count: int) -> None:
    template_id = Drive(env.drive_service).create_sheet("Шаблон", "root")
    env.drive_service.spreadsheets[template_id] = [
        _template_tab(1, "Утро"),
        _template_tab(2, "Обед"),
        _template_tab(3, "Вечер"),
    ]
    await env.seed("morning", [(f"shop{i}", i, {}) for i in range(1, count + 1)])

    spreadsheet_id, _ = await env.build(template_id=template_id)

    morning = env.sheet(spreadsheet_id, "Утро")
    last = count + 1  # строка «Итого»
    assert morning.row(last) == [TOTAL_LABEL, count]
    assert morning.formats[0] == "заголовок"
    assert all(morning.formats.get(r) == "строка" for r in range(1, last + 1))
    assert all(r not in morning.formats for r in range(last + 1, 10))
    assert (0, last + 1) in morning.resized
    # пустой лист: только «Итого: 0» в рамке, пустые рамки примеров сняты
    noon = env.sheet(spreadsheet_id, "Обед")
    assert noon.row(1) == [TOTAL_LABEL, 0]
    assert sorted(noon.formats) == [0, 1]
