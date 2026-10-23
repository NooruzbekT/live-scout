from datetime import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ttmon.settings import (
    CONFIG_FILE,
    CONFIG_OVERLAY_ENV,
    PROJECT_ROOT,
    AppConfig,
    Settings,
    load_config,
    merge,
)


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


@pytest.mark.parametrize(("host", "loopback"), [("0.0.0.0", False), ("127.0.0.1", True)])
def test_web_host_any_ip(raw_config: dict[str, Any], host: str, loopback: bool) -> None:
    raw_config["web"]["host"] = host
    assert AppConfig.model_validate(raw_config).web.is_loopback is loopback


def test_web_host_must_be_ip(raw_config: dict[str, Any]) -> None:
    raw_config["web"]["host"] = "panel.example.kz"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw_config)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://panel.example.kz/", "https://panel.example.kz"),
        ("http://127.0.0.1:18765", "http://127.0.0.1:18765"),
    ],
)
def test_public_url_reduced_to_origin(raw_config: dict[str, Any], url: str, origin: str) -> None:
    raw_config["web"]["public_url"] = url
    assert AppConfig.model_validate(raw_config).web.public_url == origin


@pytest.mark.parametrize(
    "url", ["panel.example.kz", "ftp://panel.example.kz", "https://x.kz/ttmon", "http://x.kz"]
)
def test_public_url_rejected(raw_config: dict[str, Any], url: str) -> None:
    raw_config["web"]["public_url"] = url
    with pytest.raises(ValidationError, match="public_url"):
        AppConfig.model_validate(raw_config)


def test_merge_overrides_leaves_and_keeps_siblings() -> None:
    base = {"web": {"host": "127.0.0.1", "port": 8765}, "list": [1, 2]}
    assert merge(base, {"web": {"host": "0.0.0.0"}, "list": [3]}) == {
        "web": {"host": "0.0.0.0", "port": 8765},
        "list": [3],
    }


def test_docker_overlay_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_OVERLAY_ENV, "docker/config.docker.yaml")
    config = load_config()
    assert config.web.host == "0.0.0.0" and config.web.port == 8765
    assert config.browser.login_args == ["--no-sandbox"]
    assert config.media.font_path.name == "DejaVuSans.ttf"
    assert config.slots["morning"].start == time(9, 55)


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
