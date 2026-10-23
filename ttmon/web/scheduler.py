import datetime as dt
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ttmon.media.layout import AUDIO_DIR
from ttmon.settings import AppConfig, project_path
from ttmon.timeutil import format_report_date, today_almaty
from ttmon.web.jobs import JobRunner, ttmon_command

log = structlog.get_logger(__name__)

# компьютер проснулся в 10:20 — утренний сбор всё равно стартует (misfire_grace_time)
MISFIRE_GRACE_S = 1800
JOB_DEFAULTS = {"misfire_grace_time": MISFIRE_GRACE_S, "coalesce": True, "max_instances": 1}
REPORT_TIMEOUT_S = 15 * 60
LOGS_KEEP_DAYS = 30
AUDIO_KEEP_DAYS = 7
CLEANUP_TIME = (3, 0)
# дата в имени — логи; звук (дата в папке) и скриншоты ошибок (20261021T…Z) — по mtime
DATED_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

ReportDone = Callable[[dt.date], Awaitable[bool]]
SlotStarted = Callable[[dt.date, str], Awaitable[bool]]


def collect_timeout_s(cfg: AppConfig) -> float:
    # max_minutes сбор соблюдает сам; запас — старт Chrome и загрузка на Drive после слота
    return (cfg.collect.max_minutes + 10) * 60


async def collect_slot(cfg: AppConfig, runner: JobRunner, slot: str) -> None:
    await runner.run(
        f"collect_{slot}", ttmon_command("collect", "--slot", slot), collect_timeout_s(cfg)
    )


async def build_report(runner: JobRunner, day: dt.date) -> None:
    await runner.run(
        f"report_{day.isoformat()}",
        ttmon_command("report", "--date", day.isoformat()),
        REPORT_TIMEOUT_S,
    )


async def evening(cfg: AppConfig, runner: JobRunner, slot: str) -> None:
    await collect_slot(cfg, runner, slot)
    if cfg.report.auto_after_evening:
        await build_report(runner, today_almaty(cfg.tz))


async def report_fallback(cfg: AppConfig, runner: JobRunner, report_done: ReportDone) -> None:
    day = today_almaty(cfg.tz)
    if await report_done(day):
        return
    log.info("report_fallback", date=format_report_date(day))
    await build_report(runner, day)


def _older_than(path: Path, cutoff: dt.date) -> bool:
    match = DATED_NAME.search(path.name)
    if match:
        try:
            return dt.date.fromisoformat(match.group(1)) < cutoff
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(path.stat().st_mtime).date() < cutoff


def cleanup(cfg: AppConfig, today: dt.date) -> int:
    """Логи и скриншоты ошибок старше 30 дней, звук старше 7. Открытый сегодняшний лог
    не трогаем: по дате в имени он всегда свежий."""
    removed = 0
    targets = [
        (project_path(cfg.paths.logs_dir), LOGS_KEEP_DAYS),
        (project_path(cfg.paths.data_dir) / AUDIO_DIR, AUDIO_KEEP_DAYS),
    ]
    for directory, keep in targets:
        if not directory.is_dir():
            continue
        cutoff = today - dt.timedelta(days=keep)
        for path in list(directory.rglob("*")):
            try:
                if path.is_file() and _older_than(path, cutoff):
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as exc:
                # занятый файл на Windows не должен обрывать всю уборку
                log.warning("cleanup_skipped", path=str(path), error=str(exc))
        for sub in sorted(directory.rglob("*"), reverse=True):
            if sub.is_dir() and not any(sub.iterdir()):
                shutil.rmtree(sub, ignore_errors=True)
    log.info("cleanup_done", removed=removed)
    return removed


def build_scheduler(cfg: AppConfig, runner: JobRunner, report_done: ReportDone) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=cfg.tz)
    slots = list(cfg.slots.items())
    for index, (slot, slot_cfg) in enumerate(slots):
        last = index == len(slots) - 1
        scheduler.add_job(
            evening if last else collect_slot,
            CronTrigger(hour=slot_cfg.start.hour, minute=slot_cfg.start.minute, timezone=cfg.tz),
            args=[cfg, runner, slot],
            id=f"collect_{slot}",
            **JOB_DEFAULTS,
        )
    fallback = cfg.report.fallback_time
    scheduler.add_job(
        report_fallback,
        CronTrigger(hour=fallback.hour, minute=fallback.minute, timezone=cfg.tz),
        args=[cfg, runner, report_done],
        id="report_fallback",
        **JOB_DEFAULTS,
    )
    scheduler.add_job(
        lambda: cleanup(cfg, today_almaty(cfg.tz)),
        CronTrigger(hour=CLEANUP_TIME[0], minute=CLEANUP_TIME[1], timezone=cfg.tz),
        id="cleanup",
        **JOB_DEFAULTS,
    )
    return scheduler


async def catch_up(
    scheduler: AsyncIOScheduler,
    cfg: AppConfig,
    now: dt.datetime,
    slot_started: SlotStarted,
    report_done: ReportDone,
) -> list[str]:
    """Хранилище задач в памяти: задачу, пропущенную до старта процесса (перезагрузка в 10:10,
    автозапуск при входе), APScheduler не догоняет. Слот догоняем в пределах того же окна
    misfire_grace_time, если за сегодня он ещё не запускался; страховочный отчёт — в любой
    момент после своего времени до конца дня, если вечерний отчёт ещё не собран."""
    started: list[str] = []
    grace = dt.timedelta(seconds=MISFIRE_GRACE_S)
    for slot, slot_cfg in cfg.slots.items():
        start = dt.datetime.combine(now.date(), slot_cfg.start, tzinfo=cfg.tz)
        if start <= now < start + grace and not await slot_started(now.date(), slot):
            scheduler.modify_job(f"collect_{slot}", next_run_time=now)
            started.append(f"collect_{slot}")
    fallback = dt.datetime.combine(now.date(), cfg.report.fallback_time, tzinfo=cfg.tz)
    if now >= fallback and not await report_done(now.date()):
        scheduler.modify_job("report_fallback", next_run_time=now)
        started.append("report_fallback")
    if started:
        log.info("jobs_caught_up", jobs=started)
    return started
