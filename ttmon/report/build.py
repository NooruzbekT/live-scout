import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.ai.prices import format_prices
from ttmon.ai.schema import Price
from ttmon.db.models import Report, Stream
from ttmon.google.drive import SHEET_MIME, Drive, file_url
from ttmon.google.sheets import Cell, Link, Sheets, spreadsheet_url
from ttmon.settings import AppConfig, SlotConfig, project_path
from ttmon.timeutil import format_report_date, now_utc, to_almaty

log = structlog.get_logger(__name__)

TEST_SLOT = "test"
HEADER: list[Cell] = [
    "Дата",
    "№ эфира",
    "Категория (что продают)",
    "Скриншот эфира",
    "Цены товаров (озвученные или написанные)",
]
HEADER_ROWS = 1
TOTAL_LABEL = "Итого:"
NO_SCREENSHOT = "скриншот не загружен"


@dataclass(frozen=True)
class Target:
    """Боевой отчёт или тестовый (collect --slot test): у тестового своя корневая папка,
    подпапка «Тест» и лист первого слота."""

    test: bool

    def root_folder(self, cfg: AppConfig) -> str:
        name = cfg.google.drive_root_folder_name
        return f"{name} (тест)" if self.test else name

    def slots(self, cfg: AppConfig) -> list[str]:
        return [TEST_SLOT] if self.test else list(cfg.slots)

    def sheet(self, cfg: AppConfig, slot: str) -> str:
        # тестовый слот — на лист первого слота config.yaml, на остальных только «Итого: 0»
        return next(iter(cfg.slots.values())).sheet if slot == TEST_SLOT else _slot(cfg, slot).sheet

    def slot_folder(self, cfg: AppConfig, slot: str) -> str:
        return "Тест" if slot == TEST_SLOT else _slot(cfg, slot).title


def _slot(cfg: AppConfig, slot: str) -> SlotConfig:
    for name, slot_cfg in cfg.slots.items():
        if name == slot:
            return slot_cfg
    raise KeyError(slot)


@dataclass
class ReportResult:
    url: str
    rows: dict[str, int]
    uploaded: int
    upload_failed: int


def prices_text(prices_json: str | None, no_price_text: str) -> str:
    raw = json.loads(prices_json) if prices_json else []
    return format_prices((Price.model_validate(p) for p in raw), no_price_text)


def sheet_rows(streams: list[Stream], tz: ZoneInfo, no_price_text: str) -> list[list[Cell]]:
    """№ — порядковый 1..N внутри листа: дыры от ручных исключений не видны."""
    rows: list[list[Cell]] = []
    for number, stream in enumerate(streams, start=1):
        local = to_almaty(stream.captured_at, tz)
        screenshot: Cell = (
            Link(f"Скриншот {local:%H:%M}", stream.drive_url) if stream.drive_url else NO_SCREENSHOT
        )
        rows.append(
            [
                format_report_date(stream.date),
                number,
                stream.category or "",
                screenshot,
                prices_text(stream.prices_json, no_price_text),
            ]
        )
    rows.append([TOTAL_LABEL, len(streams)])
    return rows


async def report_streams(session: AsyncSession, day: dt.date, slots: list[str]) -> list[Stream]:
    rows = await session.execute(
        select(Stream)
        .where(
            Stream.date == day,
            Stream.slot.in_(slots),
            Stream.is_selling.is_(True),
            Stream.excluded.is_(False),
        )
        # n NULL — в конец: SQLite ставит NULL первыми
        .order_by(Stream.slot, Stream.n.is_(None), Stream.n, Stream.id)
    )
    return list(rows.scalars())


async def upload_screenshots(
    session: AsyncSession,
    drive: Drive,
    cfg: AppConfig,
    target: Target,
    day: dt.date,
    slots: list[str],
) -> tuple[int, int]:
    """Скриншоты продающих без drive_file_id. Коммит после каждого файла: упавший посередине
    прогон не теряет уже загруженное. Ошибка одного файла не останавливает остальные."""
    pending = [s for s in await report_streams(session, day, slots) if s.drive_file_id is None]
    if not pending:
        return 0, 0
    root = drive.folder(target.root_folder(cfg), public=True)
    day_folder = drive.folder(day.isoformat(), root)
    uploaded = failed = 0
    for stream in pending:
        path = project_path(Path(stream.screenshot_path))
        if not path.is_file():
            log.warning("screenshot_missing", username=stream.username, path=str(path))
            failed += 1
            continue
        try:
            folder = drive.folder(target.slot_folder(cfg, stream.slot), day_folder)
            file_id = drive.upload_jpeg(path, path.name, folder)
        except Exception as exc:
            log.warning(
                "screenshot_upload_failed", username=stream.username, error=type(exc).__name__
            )
            failed += 1
            continue
        stream.drive_file_id, stream.drive_url = file_id, file_url(file_id)
        await session.commit()
        uploaded += 1
    log.info("screenshots_uploaded", uploaded=uploaded, failed=failed)
    return uploaded, failed


async def _spreadsheet(
    session: AsyncSession,
    drive: Drive,
    sheets: Sheets,
    cfg: AppConfig,
    day: dt.date,
    title: str,
    day_folder: str,
    template_id: str,
) -> str:
    """Повторный отчёт за дату — тот же файл: по Report в БД, иначе по имени в папке
    дня (БД тестового слота пересоздаётся); удалённый вручную создаётся заново."""
    existing = await _report(session, day)
    if existing is not None and drive.exists(existing.spreadsheet_id):
        return existing.spreadsheet_id
    found = drive.find(title, day_folder, SHEET_MIME)
    if found is not None:
        spreadsheet_id = found
    elif template_id:
        spreadsheet_id = drive.copy(template_id, title, day_folder)
    else:
        log.warning("report_without_template")
        spreadsheet_id = drive.create_sheet(title, day_folder)
    if not template_id:
        sheets.ensure_sheets(spreadsheet_id, [s.sheet for s in cfg.slots.values()], HEADER)
    # сразу в БД: сбой дальше (лист не найден, сеть) не плодит копии при повторе
    await _save_report(session, day, spreadsheet_id, {})
    return spreadsheet_id


async def _report(session: AsyncSession, day: dt.date) -> Report | None:
    return (await session.execute(select(Report).where(Report.date == day))).scalar_one_or_none()


async def _save_report(
    session: AsyncSession, day: dt.date, spreadsheet_id: str, rows: dict[str, int]
) -> Report:
    report = await _report(session, day)
    url = spreadsheet_url(spreadsheet_id)
    if report is None:
        report = Report(date=day, spreadsheet_id=spreadsheet_id, url=url, built_at=now_utc())
        session.add(report)
    report.spreadsheet_id, report.url, report.built_at = spreadsheet_id, url, now_utc()
    report.rows_morning = rows.get("morning", 0)
    report.rows_noon = rows.get("noon", 0)
    report.rows_evening = rows.get("evening", 0)
    await session.commit()
    return report


async def build_report(
    session: AsyncSession,
    drive: Drive,
    sheets: Sheets,
    cfg: AppConfig,
    day: dt.date,
    target: Target,
    template_id: str,
) -> ReportResult:
    slots = target.slots(cfg)
    uploaded, upload_failed = await upload_screenshots(session, drive, cfg, target, day, slots)
    root = drive.folder(target.root_folder(cfg), public=True)
    day_folder = drive.folder(day.isoformat(), root)
    title = cfg.google.report_title.format(date=format_report_date(day))
    if target.test:
        title = f"ТЕСТ {title}"
    spreadsheet_id = await _spreadsheet(
        session, drive, sheets, cfg, day, title, day_folder, template_id
    )

    streams = await report_streams(session, day, slots)
    # все листы: строки-примеры шаблона и прошлый прогон стираются и там, где эфиров нет
    data = {s.sheet: sheet_rows([], cfg.tz, cfg.report.no_price_text) for s in cfg.slots.values()}
    per_slot: dict[str, int] = {}
    for slot in slots:
        slot_streams = [s for s in streams if s.slot == slot]
        data[target.sheet(cfg, slot)] = sheet_rows(slot_streams, cfg.tz, cfg.report.no_price_text)
        per_slot[slot] = len(slot_streams)
    sheets.write(spreadsheet_id, data, HEADER_ROWS)
    if cfg.google.share_with:
        try:
            drive.share_with(spreadsheet_id, cfg.google.share_with)
        except Exception as exc:
            # таблица и так открыта по ссылке; адрес не Google-аккаунта Drive без письма отклоняет
            log.warning("report_share_failed", error=type(exc).__name__)

    # тестовый слот лежит на листе первого слота — и в счётчике тоже
    rows = dict(per_slot)
    if TEST_SLOT in rows:
        rows[next(iter(cfg.slots))] = rows.pop(TEST_SLOT)
    report = await _save_report(session, day, spreadsheet_id, rows)
    log.info("report_built", date=day.isoformat(), rows=per_slot, test=target.test)
    return ReportResult(report.url, per_slot, uploaded, upload_failed)
