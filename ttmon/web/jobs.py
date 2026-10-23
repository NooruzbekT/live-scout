import asyncio
import contextlib
import datetime as dt
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ttmon.settings import PROJECT_ROOT
from ttmon.timeutil import now_utc

log = structlog.get_logger(__name__)

TAIL_LINES = 30
TAIL_LINE_CHARS = 500
READ_CHUNK = 65536
EXIT_TIMEOUT = -1
EXIT_START_FAILED = -2
EXIT_FAILED = -3
# полный путь: поиск по имени на Windows начинается с текущей папки
TASKKILL = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"


class JobBusy(RuntimeError):
    pass


@dataclass
class JobResult:
    name: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    exit_code: int | None = None
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL_LINES))


def ttmon_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "ttmon", *args]


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """collect запускает Chrome: kill одного python оставил бы браузер с открытым профилем.
    На Linux задача — лидер своей группы (start_new_session): killpg убивает python, драйвер
    Playwright и ffmpeg; Chrome Playwright держит в своей группе, он закрывается сам по обрыву
    pipe драйвера."""
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            str(TASKKILL),
            "/T",
            "/F",
            "/PID",
            str(process.pid),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


class JobRunner:
    """Задачи (сбор, отчёт) — отдельными процессами: collect/report синхронные (свой
    asyncio.run), а падение или зависание Chrome не должно ронять сервер. Одна задача за раз:
    профиль Chrome и квота AI общие."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._reserved = False
        self._tasks: set[asyncio.Task[JobResult]] = set()
        self._process: asyncio.subprocess.Process | None = None
        self.current: JobResult | None = None
        self.last: dict[str, JobResult] = {}

    @property
    def busy(self) -> bool:
        return self._lock.locked() or self._reserved

    async def run(self, name: str, command: Sequence[str], timeout_s: float) -> JobResult:
        """Ждёт освобождения (задачи планировщика встают в очередь)."""
        async with self._lock:
            return await self._run(name, command, timeout_s)

    def start(
        self, name: str, command: Sequence[str], timeout_s: float
    ) -> "asyncio.Task[JobResult]":
        """Ручной запуск из панели: в фоне, без очереди. Бронь ставится синхронно — второй
        клик до захвата блокировки тоже получает JobBusy."""
        if self.busy:
            raise JobBusy(self.current.name if self.current else "задача")
        self._reserved = True

        async def go() -> JobResult:
            async with self._lock:
                self._reserved = False
                return await self._run(name, command, timeout_s)

        task = asyncio.create_task(go())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Остановка сервера посреди сбора: иначе Chrome остался бы с открытым профилем."""
        if self._process is not None and self._process.returncode is None:
            log.warning("job_killed_on_shutdown", job=self.current.name if self.current else None)
            await _kill_tree(self._process)

    async def _run(self, name: str, command: Sequence[str], timeout_s: float) -> JobResult:
        result = JobResult(name=name, started_at=now_utc())
        self.current = result
        log.info("job_started", job=name)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=PROJECT_ROOT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            log.error("job_start_failed", job=name, error=str(exc))
            result.tail.append(f"не запустился: {exc}")
            result.exit_code = EXIT_START_FAILED
            return self._finish(result)
        process = self._process
        try:
            await asyncio.wait_for(self._read(process, result), timeout=timeout_s)
            result.exit_code = await process.wait()
        except TimeoutError:
            log.error("job_timeout", job=name, timeout_s=timeout_s)
            await _kill_tree(process)
            result.exit_code = EXIT_TIMEOUT
        except asyncio.CancelledError:
            await _kill_tree(process)
            self._finish(result)
            raise
        except Exception as exc:
            # ребёнок без читателя пайпа повиснет на записи — убиваем, задача считается упавшей
            log.exception("job_failed", job=name)
            await _kill_tree(process)
            result.tail.append(f"сбой: {exc}")
            result.exit_code = EXIT_FAILED
        return self._finish(result)

    def _finish(self, result: JobResult) -> JobResult:
        result.finished_at = now_utc()
        self._process = None
        self.current = None
        self.last[result.name] = result
        log.info("job_finished", job=result.name, exit_code=result.exit_code)
        return result

    @staticmethod
    async def _read(process: asyncio.subprocess.Process, result: JobResult) -> None:
        """Блоками, а не readline: строка длиннее лимита StreamReader (64 КБ) уронила бы чтение."""
        assert process.stdout is not None
        pending = b""
        while chunk := await process.stdout.read(READ_CHUNK):
            *lines, pending = (pending + chunk).split(b"\n")
            pending = pending[-READ_CHUNK:]
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    result.tail.append(line[:TAIL_LINE_CHARS])
        if tail := pending.decode("utf-8", errors="replace").rstrip():
            result.tail.append(tail[:TAIL_LINE_CHARS])
        await process.wait()
