import datetime as dt
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ttmon.settings import AppConfig
from ttmon.timeutil import format_report_date, to_almaty, today_almaty
from ttmon.web import service
from ttmon.web.deps import CfgDep, DbDep, RunnerDep, SchedulerDep, ShotsDep

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
SELLING_FILTER = {"true": True, "false": False}
STATUS_TITLES = {
    "running": "идёт",
    "ok": "норма",
    "partial": "меньше нормы",
    "failed": "сбой",
    "aborted": "остановлен",
}

router = APIRouter()


def job_title(cfg: AppConfig, name: str) -> str:
    kind, _, arg = name.partition("_")
    titles: dict[str, str] = {str(slot): slot_cfg.title for slot, slot_cfg in cfg.slots.items()}
    if kind == "collect" and arg in titles:
        return f"сбор «{titles[arg]}»"
    if kind == "report":
        return f"отчёт за {arg}"
    return name


def _local(cfg: AppConfig) -> Callable[..., str]:
    def fmt(moment: dt.datetime | None, pattern: str = "%H:%M") -> str:
        return to_almaty(moment, cfg.tz).strftime(pattern) if moment else "—"

    return fmt


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    db: DbDep,
    cfg: CfgDep,
    runner: RunnerDep,
    scheduler: SchedulerDep,
    shots: ShotsDep,
) -> HTMLResponse:
    day = today_almaty(cfg.tz)
    runs = {run.slot: run for run in await service.runs_for(db, day)}
    selling = [s for s in await service.streams_for(db, day, None, True) if not s.excluded]
    # по эфирам, а не Run.selling_found: учитывает правки в панели
    counts = {slot: sum(s.slot == slot for s in selling) for slot in cfg.slots}
    next_runs = {job.id: job.next_run_time for job in scheduler.get_jobs()} if scheduler else {}
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "day": day,
            "day_title": format_report_date(day),
            "slots": [(slot, slot_cfg, runs.get(slot)) for slot, slot_cfg in cfg.slots.items()],
            "streams": [service.stream_out(s, cfg, shots) for s in selling],
            "counts": counts,
            "report": await service.report_for(db, day),
            "next_runs": next_runs,
            "current": job_title(cfg, runner.current.name) if runner.current else None,
            "current_started": runner.current.started_at if runner.current else None,
            "statuses": STATUS_TITLES,
            "local": _local(cfg),
            "target": cfg.collect.min_selling,
        },
    )


@router.get("/day/{day}", response_class=HTMLResponse)
async def day_page(
    request: Request,
    day: dt.date,
    db: DbDep,
    cfg: CfgDep,
    shots: ShotsDep,
    slot: str | None = None,
    selling: str | None = None,
) -> HTMLResponse:
    # форма шлёт «все» как пустую строку: для bool-параметра это 422
    selling_flag = SELLING_FILTER.get(selling or "")
    found = await service.streams_for(db, day, slot or None, selling_flag)
    return TEMPLATES.TemplateResponse(
        request,
        "day.html",
        {
            "day": day,
            "day_title": format_report_date(day),
            "prev_day": day - dt.timedelta(days=1),
            "next_day": day + dt.timedelta(days=1),
            "slot": slot,
            "selling": selling_flag,
            "slots": cfg.slots,
            "streams": [service.stream_out(s, cfg, shots) for s in found],
            "categories": cfg.categories,
            "no_price_text": cfg.report.no_price_text,
            "report": await service.report_for(db, day),
            "local": _local(cfg),
        },
    )
