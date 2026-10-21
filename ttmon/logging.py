import logging
import re
import sys
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.types import EventDict, Processor, WrappedLogger

LOG_PREFIX = "ttmon"
PROCESS_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# без \b: в URL токен идёт сразу после "bot" (/bot123:ABC...), границы слова там нет
TELEGRAM_TOKEN = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")
# ключ Google API: в логи сейчас не попадает (уходит заголовком), маска — страховка.
# Формат ключей меняется (16.10.2026 выдан ключ не с AIza), поэтому ещё и по значению
GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")
_secrets: set[str] = set()


def register_secret(value: str) -> None:
    if len(value) >= 8:
        _secrets.add(value)


REDACTED = "***"
# INFO у httpx пишет полный URL запроса, у Telegram в нём токен
QUIET_LOGGERS = ("httpx", "httpcore")


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, REDACTED)
    return GOOGLE_API_KEY.sub(REDACTED, TELEGRAM_TOKEN.sub(REDACTED, text))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    return {k: _redact_value(v) for k, v in event_dict.items()}


class DailyFileHandler(logging.FileHandler):
    """Файл на каждый день и на каждый процесс, без переименования: на Windows rename открытого
    файла падает, а дозапись в один файл из двух процессов теряет строки."""

    def __init__(self, directory: Path, process: str, tz: tzinfo) -> None:
        if not PROCESS_NAME.match(process):
            raise ValueError(f"недопустимое имя процесса для лога: {process!r}")
        self._directory = directory
        self._process = process
        self._tz = tz
        self._day = datetime.now(tz).date()
        super().__init__(self._path_for(self._day), encoding="utf-8", delay=True)

    def _path_for(self, day: date) -> Path:
        return self._directory / f"{LOG_PREFIX}-{day.isoformat()}-{self._process}.jsonl"

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.fromtimestamp(record.created, self._tz).date()
        if day != self._day:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self._day = day
            self.baseFilename = str(self._path_for(day).resolve())
        super().emit(record)


def setup_logging(logs_dir: Path, process: str, tz: tzinfo, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # трейсбек рендерится в текст/dict до маскировки; show_locals в structlog по умолчанию True
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                redact_processor,
                structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            ],
        )
    )

    file = DailyFileHandler(logs_dir, process, tz)
    file.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
                redact_processor,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
        )
    )

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.addHandler(console)
    root.addHandler(file)
    root.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
