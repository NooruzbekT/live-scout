from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.db.models import Run, Stream
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.settings import PROJECT_ROOT


def _alembic_config(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["db_file"] = str(db_file)
    cfg.attributes["skip_logging_config"] = True
    return cfg


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "ttmon.db"
    command.upgrade(_alembic_config(path), "head")
    return path


@pytest.fixture
async def session(db_file: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(db_file)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


def _run() -> Run:
    return Run(
        date=date(2026, 10, 16),
        slot="morning",
        status="running",
        started_at=datetime(2026, 10, 16, 4, 55, tzinfo=UTC),
    )


def _stream(
    run: Run, username: str = "shop.almaty.test", room_id: str = "7001", n: int | None = None
) -> Stream:
    return Stream(
        run=run,
        date=run.date,
        slot=run.slot,
        n=n,
        username=username,
        nickname="Женская одежда Алматы",
        room_id=room_id,
        title="",
        viewers=12,
        followers=3400,
        captured_at=datetime(2026, 10, 16, 5, 7, tzinfo=UTC),
        url="https://www.tiktok.com/@shop.almaty.test/live",
        source="search:одежда алматы",
        screenshot_path="data/screenshots/x.jpg",
        chat_json="[]",
    )


def test_models_match_migrations(db_file: Path) -> None:
    command.check(_alembic_config(db_file))


async def test_pragmas(session: AsyncSession) -> None:
    assert (await session.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
    assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1


async def test_same_seller_twice_in_slot_rejected(session: AsyncSession) -> None:
    run = _run()
    session.add_all([_stream(run, room_id="7001"), _stream(run, room_id="7002")])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_duplicate_n_in_run_rejected(session: AsyncSession) -> None:
    run = _run()
    session.add_all([_stream(run, "a", n=1), _stream(run, "b", n=1)])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_many_not_selling_streams_without_n(session: AsyncSession) -> None:
    run = _run()
    session.add_all([_stream(run, "a"), _stream(run, "b"), _stream(run, "c", n=1)])
    await session.commit()
    assert run.rooms_failed == 0


async def test_orm_delete_run_cascades(session: AsyncSession) -> None:
    run = _run()
    session.add_all([_stream(run, "a"), _stream(run, "b")])
    await session.commit()

    await session.delete(run)
    await session.commit()

    assert (await session.execute(select(Stream))).scalars().all() == []


async def test_duplicate_run_rejected(session: AsyncSession) -> None:
    session.add_all([_run(), _run()])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_datetime_roundtrip_is_aware_utc(session: AsyncSession) -> None:
    run = _run()
    run.started_at = datetime(2026, 10, 16, 9, 55, tzinfo=timezone(timedelta(hours=5)))
    session.add(run)
    await session.commit()
    session.expunge_all()

    loaded = (await session.execute(select(Run))).scalar_one()
    assert loaded.started_at == datetime(2026, 10, 16, 4, 55, tzinfo=UTC)
    assert loaded.started_at.tzinfo is UTC


async def test_naive_datetime_rejected(session: AsyncSession) -> None:
    run = _run()
    run.started_at = datetime(2026, 10, 16, 4, 55)
    session.add(run)
    with pytest.raises(StatementError, match="naive"):
        await session.commit()
