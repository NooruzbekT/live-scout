from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.db import repo
from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Run, RunStatus, Stream
from ttmon.db.session import create_engine, make_sessionmaker

DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 4, 55, tzinfo=UTC)


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "ttmon.db"
    upgrade_to_head(path)
    return path


@pytest.fixture
async def session(db_file: Path) -> AsyncIterator[AsyncSession]:
    engine = create_engine(db_file)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


def _stream(run: Run, username: str, n: int | None = None) -> Stream:
    return Stream(
        run_id=run.id,
        date=run.date,
        slot=run.slot,
        n=n,
        username=username,
        nickname="",
        room_id=username,
        title="",
        viewers=0,
        followers=0,
        captured_at=NOW,
        url="",
        source="search:x",
        screenshot_path="",
        chat_json="[]",
    )


async def test_rerun_continues_same_run(session: AsyncSession) -> None:
    first = await repo.get_or_create_run(session, DAY, "morning", NOW)
    first.status = RunStatus.ABORTED
    first.error = "капча"
    first.rooms_opened = 7
    await session.commit()

    again = await repo.get_or_create_run(session, DAY, "morning", NOW)

    assert again.id == first.id
    assert again.status == RunStatus.RUNNING
    assert again.error is None
    assert again.rooms_opened == 7


async def test_seen_usernames_and_next_n(session: AsyncSession) -> None:
    run = await repo.get_or_create_run(session, DAY, "noon", NOW)
    other = await repo.get_or_create_run(session, DAY, "evening", NOW)
    session.add_all(
        [_stream(run, "a", n=1), _stream(run, "b"), _stream(run, "c", n=2), _stream(other, "z", 5)]
    )
    await session.commit()

    assert await repo.seen_usernames(session, run.id) == {"a", "b", "c"}
    assert await repo.next_n(session, run.id) == 3
    assert (
        await repo.next_n(session, (await repo.get_or_create_run(session, DAY, "x", NOW)).id) == 1
    )
