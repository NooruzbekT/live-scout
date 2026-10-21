import datetime as dt
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from ttmon.timeutil import today_almaty
from ttmon.web import service
from ttmon.web.deps import CfgDep, DbDep, RunnerDep, SchedulerDep, ShotsDep
from ttmon.web.jobs import JobBusy, JobResult, ttmon_command
from ttmon.web.scheduler import REPORT_TIMEOUT_S, collect_timeout_s
from ttmon.web.schemas import (
    HealthOut,
    JobOut,
    JobStarted,
    RunOut,
    StreamOut,
    StreamPatch,
)

router = APIRouter()

DateQuery = Annotated[dt.date | None, Query(alias="date")]


def job_out(job: JobResult) -> JobOut:
    return JobOut(
        name=job.name,
        started_at=job.started_at,
        finished_at=job.finished_at,
        exit_code=job.exit_code,
        tail=list(job.tail),
    )


def _busy(exc: JobBusy) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, f"уже выполняется: {exc}")


@router.get("/health", response_model=HealthOut)
async def health(db: DbDep, runner: RunnerDep, scheduler: SchedulerDep) -> HealthOut:
    jobs = scheduler.get_jobs() if scheduler else []
    return HealthOut(
        ok=True,
        logged_in=await service.logged_in(db),
        running_job=runner.current.name if runner.current else None,
        next_runs={job.id: job.next_run_time for job in jobs},
    )


@router.get("/runs", response_model=list[RunOut])
async def runs(db: DbDep, cfg: CfgDep, day: DateQuery = None) -> list[RunOut]:
    found = await service.runs_for(db, day or today_almaty(cfg.tz))
    return [RunOut.model_validate(run) for run in found]


@router.post("/runs/{slot}", response_model=JobStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_run(slot: str, cfg: CfgDep, runner: RunnerDep) -> JobStarted:
    if slot not in cfg.slots:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"нет слота {slot!r}")
    name = f"collect_{slot}"
    try:
        runner.start(name, ttmon_command("collect", "--slot", slot), collect_timeout_s(cfg))
    except JobBusy as exc:
        raise _busy(exc) from exc
    return JobStarted(job=name)


@router.get("/streams", response_model=list[StreamOut])
async def streams(
    db: DbDep,
    cfg: CfgDep,
    shots: ShotsDep,
    day: DateQuery = None,
    slot: str | None = None,
    selling: bool | None = None,
) -> list[StreamOut]:
    found = await service.streams_for(db, day or today_almaty(cfg.tz), slot, selling)
    return [service.stream_out(s, cfg, shots) for s in found]


@router.patch("/streams/{stream_id}", response_model=StreamOut)
async def patch_stream(
    stream_id: int, patch: StreamPatch, db: DbDep, cfg: CfgDep, shots: ShotsDep
) -> StreamOut:
    try:
        stream = await service.patch_stream(db, stream_id, patch, cfg)
    except service.PatchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if stream is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "эфир не найден")
    return service.stream_out(stream, cfg, shots)


@router.post("/reports/{day}", response_model=JobStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_report(day: dt.date, runner: RunnerDep) -> JobStarted:
    name = f"report_{day.isoformat()}"
    try:
        runner.start(name, ttmon_command("report", "--date", day.isoformat()), REPORT_TIMEOUT_S)
    except JobBusy as exc:
        raise _busy(exc) from exc
    return JobStarted(job=name)


@router.get("/jobs", response_model=list[JobOut])
async def jobs(runner: RunnerDep) -> list[JobOut]:
    found = [runner.current] if runner.current else []
    found += sorted(runner.last.values(), key=lambda j: j.started_at, reverse=True)
    return [job_out(job) for job in found]
