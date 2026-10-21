from datetime import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ttmon.settings import CONFIG_FILE, PROJECT_ROOT, AppConfig, Settings, load_config


@pytest.fixture
def raw_config() -> dict[str, Any]:
    with CONFIG_FILE.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def test_project_config_is_valid() -> None:
    config = load_config()
    assert set(config.slots) == {"morning", "noon", "evening"}
    assert config.slots["morning"].start == time(9, 55)
    assert config.report.fallback_time == time(19, 30)
    assert config.tz.key == "Asia/Almaty"


def test_unquoted_yaml_time_rejected(raw_config: dict[str, Any]) -> None:
    raw_config["slots"]["noon"]["start"] = yaml.safe_load("13:55")
    assert raw_config["slots"]["noon"]["start"] == 835
    with pytest.raises(ValidationError, match="HH:MM"):
        AppConfig.model_validate(raw_config)


def test_reversed_range_rejected(raw_config: dict[str, Any]) -> None:
    raw_config["collect"]["watch_seconds"] = [14, 8]
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_non_loopback_host_rejected(raw_config: dict[str, Any], host: str) -> None:
    raw_config["web"]["host"] = host
    with pytest.raises(ValidationError, match="loopback"):
        AppConfig.model_validate(raw_config)


def test_unknown_key_rejected(raw_config: dict[str, Any]) -> None:
    raw_config["collect"]["max_minuts"] = 45
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


def test_missing_slot_rejected(raw_config: dict[str, Any]) -> None:
    del raw_config["slots"]["noon"]
    with pytest.raises(ValidationError, match="noon"):
        AppConfig.model_validate(raw_config)


def test_fallback_category_required(raw_config: dict[str, Any]) -> None:
    raw_config["categories"].remove("Другое")
    with pytest.raises(ValidationError, match="Другое"):
        AppConfig.model_validate(raw_config)


def test_unknown_timezone_rejected(raw_config: dict[str, Any]) -> None:
    raw_config["timezone"] = "Asia/Nowhere"
    with pytest.raises(ValidationError, match="часовой пояс"):
        AppConfig.model_validate(raw_config)


@pytest.mark.parametrize("title", ["Отчёт {data}", "Отчёт {0}", "Отчёт {date"])
def test_bad_report_title_rejected(raw_config: dict[str, Any], title: str) -> None:
    raw_config["google"]["report_title"] = title
    with pytest.raises(ValidationError, match="report_title"):
        AppConfig.model_validate(raw_config)


def test_google_files_resolved_from_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path.home())
    settings = Settings(_env_file=None)
    assert settings.google_token_file == PROJECT_ROOT / "secrets" / "token.json"


def test_empty_env_values_are_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    for name in ("GEMINI_API_KEY", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=env_file)

    assert settings.gemini_api_key is None
    assert settings.telegram_chat_id == "42"
