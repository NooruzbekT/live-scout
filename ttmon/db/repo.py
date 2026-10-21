import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.db.models import Run, RunStatus, Stream


async def get_or_create_run(
    session: AsyncSession, day: dt.date, slot: str, now: dt.datetime
) -> Run:
    """Перезапуск слота продолжает тот же Run: счётчики и уже снятые эфиры сохраняются."""
    run = (
        await session.execute(select(Run).where(Run.date == day, Run.slot == slot))
    ).scalar_one_or_none()
    if run is None:
        run = Run(date=day, slot=slot, status=RunStatus.RUNNING, started_at=now)
        session.add(run)
    else:
        run.status = RunStatus.RUNNING
        run.finished_at = None
        run.error = None
    await session.commit()
    return run


async def seen_usernames(session: AsyncSession, run_id: int) -> set[str]:
    rows = await session.execute(select(Stream.username).where(Stream.run_id == run_id))
    return set(rows.scalars())


async def count_streams(session: AsyncSession, run_id: int) -> int:
    return await session.scalar(select(func.count()).where(Stream.run_id == run_id)) or 0


async def next_n(session: AsyncSession, run_id: int) -> int:
    current = await session.scalar(select(func.max(Stream.n)).where(Stream.run_id == run_id))
    return (current or 0) + 1
