from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ttmon.settings import AppConfig
from ttmon.web.jobs import JobRunner


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


def get_cfg(request: Request) -> AppConfig:
    cfg: AppConfig = request.app.state.cfg
    return cfg


def get_runner(request: Request) -> JobRunner:
    runner: JobRunner = request.app.state.runner
    return runner


def get_screenshots_dir(request: Request) -> Path:
    path: Path = request.app.state.screenshots_dir
    return path


def get_scheduler(request: Request) -> AsyncIOScheduler | None:
    scheduler: AsyncIOScheduler | None = request.app.state.scheduler
    return scheduler


DbDep = Annotated[AsyncSession, Depends(get_db)]
CfgDep = Annotated[AppConfig, Depends(get_cfg)]
RunnerDep = Annotated[JobRunner, Depends(get_runner)]
ShotsDep = Annotated[Path, Depends(get_screenshots_dir)]
SchedulerDep = Annotated[AsyncIOScheduler | None, Depends(get_scheduler)]
