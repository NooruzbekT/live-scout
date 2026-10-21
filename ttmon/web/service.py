import datetime as dt
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.ai.prices import parse_amount
from ttmon.ai.schema import Currency, Price
from ttmon.collector.guards import PageState
from ttmon.db import repo
from ttmon.db.models import Report, Run, RunStatus, Stream
from ttmon.report.build import prices_text
from ttmon.settings import AppConfig, project_path
from ttmon.web.schemas import StreamOut, StreamPatch

MEDIA_URL = "/media/screenshots"
N_RETRIES = 3
# как format_prices пишет не-тенге («3 000 USD»), плюс привычные обозначения
CURRENCY_MARKS: list[tuple[re.Pattern[str], Currency]] = [
    (re.compile(r"USD|\$|долл", re.IGNORECASE), "USD"),
    (re.compile(r"RUB|₽|руб", re.IGNORECASE), "RUB"),
    (re.compile(r"OTHER", re.IGNORECASE), "OTHER"),
]


class PatchError(ValueError):
    pass


def parse_prices_text(text: str) -> list[Price]:
    """«Пижама 5 шт: 12 500; Кроссовки: 120 USD; 1500» → цены с source=manual. Валюта —
    по отметке в сумме, иначе тенге: панель показывает не-тенге с кодом, правка соседней цены
    не должна превратить доллары в тенге."""
    prices: list[Price] = []
    for part in (p.strip() for p in text.split(";")):
        if not part:
            continue
        item, sep, value = part.rpartition(":")
        value = value if sep else part
        amount = parse_amount(value)
        if amount is None:
            raise PatchError(f"не понял сумму в «{part}»")
        found = (code for mark, code in CURRENCY_MARKS if mark.search(value))
        currency: Currency = next(found, "KZT")
        prices.append(
            Price(item=item.strip() or None, amount=amount, currency=currency, source="manual")
        )
    return prices


def screenshot_url(stream: Stream, screenshots_dir: Path) -> str | None:
    path = project_path(Path(stream.screenshot_path)).resolve()
    root = screenshots_dir.resolve()
    if not path.is_relative_to(root):
        return None
    return f"{MEDIA_URL}/{path.relative_to(root).as_posix()}"


def stream_out(stream: Stream, cfg: AppConfig, screenshots_dir: Path) -> StreamOut:
    return StreamOut(
        id=stream.id,
        date=stream.date,
        slot=stream.slot,
        n=stream.n,
        username=stream.username,
        nickname=stream.nickname,
        url=stream.url,
        captured_at=stream.captured_at,
        is_selling=stream.is_selling,
        confidence=stream.confidence,
        category=stream.category,
        prices_text=prices_text(stream.prices_json, cfg.report.no_price_text),
        ai_reason=stream.ai_reason,
        manual_override=stream.manual_override,
        excluded=stream.excluded,
        screenshot_url=screenshot_url(stream, screenshots_dir),
        drive_url=stream.drive_url,
    )


async def runs_for(session: AsyncSession, day: dt.date) -> list[Run]:
    rows = await session.execute(select(Run).where(Run.date == day).order_by(Run.started_at))
    return list(rows.scalars())


async def streams_for(
    session: AsyncSession, day: dt.date, slot: str | None, selling: bool | None
) -> list[Stream]:
    query = select(Stream).where(Stream.date == day)
    if slot:
        query = query.where(Stream.slot == slot)
    if selling is not None:
        query = query.where(Stream.is_selling.is_(selling))
    # n NULL — в конец: SQLite ставит NULL первыми
    query = query.order_by(Stream.slot, Stream.n.is_(None), Stream.n, Stream.captured_at)
    return list((await session.execute(query)).scalars())


async def report_for(session: AsyncSession, day: dt.date) -> Report | None:
    return (await session.execute(select(Report).where(Report.date == day))).scalar_one_or_none()


async def report_built_after_evening(session: AsyncSession, day: dt.date, evening: str) -> bool:
    """Отчёт, собранный вручную днём, вечерних эфиров не содержит: страховочная сборка
    пропускается, только если отчёт новее окончания вечернего слота (или слота не было)."""
    report = await report_for(session, day)
    if report is None:
        return False
    run = next((r for r in await runs_for(session, day) if r.slot == evening), None)
    if run is None:
        return True
    finished = run.finished_at or run.started_at
    return report.built_at >= finished


def _apply(stream: Stream, patch: StreamPatch, cfg: AppConfig) -> None:
    if patch.category is not None:
        if patch.category not in cfg.categories:
            raise PatchError(f"категория не из списка: {patch.category!r}")
        stream.category = patch.category
    if patch.prices_text is not None:
        prices = parse_prices_text(patch.prices_text)
        stream.prices_json = json.dumps([p.model_dump() for p in prices], ensure_ascii=False)
    if patch.is_selling is not None:
        stream.is_selling = patch.is_selling
    if patch.excluded is not None:
        stream.excluded = patch.excluded
    stream.manual_override = True


async def patch_stream(
    session: AsyncSession, stream_id: int, patch: StreamPatch, cfg: AppConfig
) -> Stream | None:
    """Правка из панели; в отчёт попадёт при следующей сборке. manual_override — пометка
    ручной правки для панели и разбора качества AI."""
    for attempt in range(N_RETRIES):
        stream = await session.get(Stream, stream_id)
        if stream is None:
            return None
        _apply(stream, patch, cfg)
        if patch.is_selling and stream.n is None:
            # № нужен для порядка в отчёте; max+1 параллельно считает и идущий сбор этого слота
            stream.n = await repo.next_n(session, stream.run_id)
        try:
            await session.commit()
            return stream
        except IntegrityError:
            await session.rollback()
            if attempt == N_RETRIES - 1:
                raise
    raise AssertionError("unreachable")


async def logged_in(session: AsyncSession) -> bool | None:
    """По последнему завершённому запуску: None — запусков ещё не было."""
    last = (
        (
            await session.execute(
                select(Run).where(Run.status != RunStatus.RUNNING).order_by(Run.started_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if last is None:
        return None
    # run.error из StopSlot начинается с PageState (collector.guards.StopSlot)
    return PageState.LOGGED_OUT.value not in (last.error or "")
