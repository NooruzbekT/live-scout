import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ttmon.db.session import create_engine, make_sessionmaker
from ttmon.settings import AppConfig
from ttmon.timeutil import now_almaty
from ttmon.web import api, pages
from ttmon.web.guard import allowed_hosts, csrf_guard
from ttmon.web.jobs import JobRunner
from ttmon.web.scheduler import build_scheduler, catch_up
from ttmon.web.service import MEDIA_URL, report_built_after_evening, runs_for

log = structlog.get_logger(__name__)


def create_app(
    cfg: AppConfig,
    db_file: Path,
    screenshots_dir: Path,
    *,
    runner: JobRunner | None = None,
    schedule: bool = True,
) -> FastAPI:
    """БД мигрирует вызывающий: upgrade_to_head внутри вызывает asyncio.run, из работающего
    loop нельзя."""
    job_runner = runner or JobRunner()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(db_file)
        sessions = make_sessionmaker(engine)
        app.state.sessions = sessions

        async def report_done(day: dt.date) -> bool:
            async with sessions() as session:
                return await report_built_after_evening(session, day, list(cfg.slots)[-1])

        async def slot_started(day: dt.date, slot: str) -> bool:
            async with sessions() as session:
                return any(run.slot == slot for run in await runs_for(session, day))

        scheduler = build_scheduler(cfg, job_runner, report_done) if schedule else None
        app.state.scheduler = scheduler
        if scheduler is not None:
            scheduler.start()
            log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
            await catch_up(scheduler, cfg, now_almaty(cfg.tz), slot_started, report_done)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            await job_runner.shutdown()
            await engine.dispose()

    app = FastAPI(title="TT Live Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.middleware("http")(csrf_guard(cfg.web))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(cfg.web))
    app.state.cfg = cfg
    app.state.runner = job_runner
    app.state.screenshots_dir = screenshots_dir
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    app.mount(MEDIA_URL, StaticFiles(directory=screenshots_dir), name="screenshots")
    app.include_router(api.router, prefix="/api")
    app.include_router(pages.router)
    return app
