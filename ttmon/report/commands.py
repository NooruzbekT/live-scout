import asyncio
import datetime as dt
from pathlib import Path

import google.auth.exceptions
import httplib2
import structlog
from googleapiclient.errors import HttpError
from sqlalchemy import select

from ttmon.db.migrate import upgrade_to_head
from ttmon.db.models import Run
from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.google.auth import GoogleAuthError, services
from ttmon.google.sheets import SheetsError
from ttmon.logging import redact
from ttmon.report.build import TEST_SLOT, ReportResult, Target, build_report, upload_screenshots
from ttmon.settings import AppConfig, Settings, project_path

log = structlog.get_logger(__name__)


class ReportError(RuntimeError):
    pass


# ошибки, которые `report` показывает текстом с кодом 12; OSError — сеть и запись token.json
GOOGLE_ERRORS: tuple[type[Exception], ...] = (
    GoogleAuthError,
    ReportError,
    SheetsError,
    HttpError,
    httplib2.HttpLib2Error,
    google.auth.exceptions.GoogleAuthError,
    OSError,
)


def error_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}".splitlines()[0])


def db_file(cfg: AppConfig, test: bool) -> Path:
    # как collector._slot_storage, но без очистки тестовой папки
    if test:
        return project_path(cfg.paths.test_data_dir) / "ttmon.db"
    return project_path(cfg.paths.db_file)


def report_command(cfg: AppConfig, settings: Settings, day: dt.date, test: bool) -> ReportResult:
    path = db_file(cfg, test)
    if not path.is_file():
        raise ReportError(f"нет базы {path}: сначала collect")
    drive, sheets = services(settings)
    upgrade_to_head(path)

    async def run() -> ReportResult:
        engine = create_engine(path)
        try:
            async with make_sessionmaker(engine)() as session:
                return await build_report(
                    session,
                    drive,
                    sheets,
                    cfg,
                    day,
                    Target(test=test),
                    settings.google_template_sheet_id,
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def upload_after_slot(cfg: AppConfig, settings: Settings, slot: str, run_id: int) -> None:
    """Сбой загрузки не роняет слот: `report` догрузит недостающее."""
    try:
        drive, _ = services(settings)
        path = db_file(cfg, slot == TEST_SLOT)

        async def run() -> tuple[int, int]:
            engine = create_engine(path)
            try:
                async with make_sessionmaker(engine)() as session:
                    slot_run = await session.scalar(select(Run).where(Run.id == run_id))
                    if slot_run is None:
                        return 0, 0
                    target = Target(test=slot == TEST_SLOT)
                    return await upload_screenshots(
                        session, drive, cfg, target, slot_run.date, [slot]
                    )
            finally:
                await engine.dispose()

        asyncio.run(run())
    except Exception as exc:
        log.warning("slot_upload_skipped", error=error_text(exc))
