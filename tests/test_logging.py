import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from ttmon.logging import DailyFileHandler, redact, register_secret, setup_logging

ALMATY = ZoneInfo("Asia/Almaty")
FAKE_TOKEN = "1234567890:AAHfakefakefakefakefakefakefakefake1"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def _records(logs_dir: Path, process: str) -> list[dict[str, Any]]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    [log_file] = logs_dir.glob(f"ttmon-*-{process}.jsonl")
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("restore_logging")
def test_jsonl_record_with_run_id(tmp_path: Path) -> None:
    setup_logging(tmp_path, "collect", ALMATY)
    structlog.contextvars.bind_contextvars(run_id=7)
    structlog.get_logger("test").info("slot_started", slot="morning", query="одежда алматы")

    record = _records(tmp_path, "collect")[-1]
    assert record["event"] == "slot_started"
    assert record["run_id"] == 7
    assert record["query"] == "одежда алматы"
    assert record["level"] == "info"


@pytest.mark.usefixtures("restore_logging")
def test_secrets_not_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(tmp_path, "collect", ALMATY)
    log = structlog.get_logger("test")

    def send(token: str) -> None:
        raise ConnectionError(f"POST https://api.telegram.org/bot{token}/sendMessage failed")

    try:
        send(FAKE_TOKEN)
    except ConnectionError:
        log.exception("telegram_failed")
    logging.getLogger("httpx").info(
        "HTTP Request: POST https://api.telegram.org/bot%s/x", FAKE_TOKEN
    )

    raw = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl"))
    assert "telegram_failed" in raw
    assert FAKE_TOKEN not in raw
    assert "AAHfake" not in raw
    assert FAKE_TOKEN not in capsys.readouterr().err


def test_daily_handler_switches_file_by_local_date(tmp_path: Path) -> None:
    handler = DailyFileHandler(tmp_path, "serve", ALMATY)
    handler.setFormatter(logging.Formatter("%(message)s"))

    def emit(moment: datetime, message: str) -> None:
        record = logging.LogRecord("t", logging.INFO, __file__, 0, message, None, None)
        record.created = moment.timestamp()
        handler.emit(record)

    emit(datetime(2026, 10, 15, 23, 59, tzinfo=ALMATY), "before")
    emit(datetime(2026, 10, 16, 0, 1, tzinfo=ALMATY), "after")
    handler.close()

    def read(name: str) -> str:
        return (tmp_path / name).read_text(encoding="utf-8").strip()

    assert read("ttmon-2026-10-15-serve.jsonl") == "before"
    assert read("ttmon-2026-10-16-serve.jsonl") == "after"


@pytest.mark.parametrize("process", ["", "../x", "Serve", "a b"])
def test_bad_process_name_rejected(tmp_path: Path, process: str) -> None:
    with pytest.raises(ValueError):
        DailyFileHandler(tmp_path, process, ALMATY)


def test_redact_registered_secret() -> None:
    # собирается в тесте: литерал, похожий на ключ, сканер секретов GitHub считает утечкой
    secret = "-".join(["fake", "value", "for", "redact"])
    register_secret(secret)
    assert redact(f"error with {secret} inside") == "error with *** inside"


def test_redact_google_api_key() -> None:
    key = "AIza" + "x" * 35
    assert redact(f"GET https://x?key={key} failed") == "GET https://x?key=*** failed"
