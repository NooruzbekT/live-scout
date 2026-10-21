import datetime as dt
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError
from typer.testing import CliRunner

import ttmon.__main__ as cli
from ttmon.collector.run import CollectResult
from ttmon.lock import exclusive_lock
from ttmon.logging import PROCESS_NAME
from ttmon.report.build import ReportResult
from ttmon.settings import Settings, load_config

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_logging_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "setup_logging", lambda *args, **kwargs: None)


def test_help_lists_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("serve", "login", "collect", "report", "eval", "doctor"):
        assert command in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["doctor"],
    ],
)
def test_stubs_exit_not_implemented(args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == cli.EXIT_NOT_IMPLEMENTED


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (cli.LoginState.LOGGED_IN, 0),
        (cli.LoginState.LOGGED_OUT, cli.EXIT_LOGGED_OUT),
        (cli.LoginState.UNKNOWN, cli.EXIT_LOGIN_UNKNOWN),
    ],
)
def test_login_check_exit_codes(
    monkeypatch: pytest.MonkeyPatch, state: cli.LoginState, code: int
) -> None:
    async def fake_check(_config: object) -> cli.LoginState:
        return state

    def must_not_open(_config: object) -> None:
        raise AssertionError("--check не должен открывать окно входа")

    monkeypatch.setattr(cli, "check_login", fake_check)
    monkeypatch.setattr(cli, "interactive_login", must_not_open)
    result = runner.invoke(cli.app, ["login", "--check"])
    assert result.exit_code == code


def test_login_opens_window_then_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_login(_config: object) -> None:
        calls.append("login")

    async def fake_check(_config: object) -> cli.LoginState:
        calls.append("check")
        return cli.LoginState.LOGGED_IN

    monkeypatch.setattr(cli, "interactive_login", fake_login)
    monkeypatch.setattr(cli, "check_login", fake_check)
    result = runner.invoke(cli.app, ["login"])
    assert result.exit_code == 0
    assert calls == ["login", "check"]


def test_login_browser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(_config: object) -> cli.LoginState:
        raise cli.BrowserLaunchError("профиль занят")

    monkeypatch.setattr(cli, "check_login", fail)
    result = runner.invoke(cli.app, ["login", "--check"])
    assert result.exit_code == cli.EXIT_BROWSER_FAILED


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (cli.RunStatus.OK, 0),
        (cli.RunStatus.PARTIAL, cli.EXIT_PARTIAL),
        (cli.RunStatus.FAILED, cli.EXIT_FAILED),
        (cli.RunStatus.ABORTED, cli.EXIT_FAILED),
    ],
)
def test_collect_no_ai_exit_codes(
    monkeypatch: pytest.MonkeyPatch, status: cli.RunStatus, code: int
) -> None:
    calls: list[tuple[str, int]] = []

    def fake_collect(_config: object, slot: str, limit: int, classifier: object) -> CollectResult:
        assert classifier is None
        calls.append((slot, limit))
        return CollectResult(run_id=1, status=status, opened=5, saved=4, failed=1)

    monkeypatch.setattr(cli, "collect_command", fake_collect)
    result = runner.invoke(cli.app, ["collect", "--slot", "test", "--limit", "5", "--no-ai"])
    assert result.exit_code == code
    assert calls == [("test", 5)]


def test_collect_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*_args: object) -> CollectResult:
        raise cli.AlreadyRunning("занято")

    monkeypatch.setattr(cli, "collect_command", busy)
    result = runner.invoke(cli.app, ["collect", "--slot", "morning", "--no-ai"])
    assert result.exit_code == cli.EXIT_ALREADY_RUNNING


def test_login_while_collect_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lock = tmp_path / "collect.lock"
    monkeypatch.setattr(cli, "collect_lock_path", lambda _cfg: lock)
    with exclusive_lock(lock):
        result = runner.invoke(cli.app, ["login", "--check"])
    assert result.exit_code == cli.EXIT_ALREADY_RUNNING


def test_collect_with_ai_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli, "collect_command", lambda *_a: pytest.fail("сбор без ключа"))
    result = runner.invoke(cli.app, ["collect", "--slot", "test"])
    assert result.exit_code == cli.EXIT_AI_CONFIG


def test_eval_without_labels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base = load_config()
    cfg = base.model_copy(
        update={"evaluation": base.evaluation.model_copy(update={"dir": tmp_path / "eval"})}
    )
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    result = runner.invoke(cli.app, ["eval"])
    assert result.exit_code == cli.EXIT_EVAL_FAILED


def test_bad_slot_is_usage_error() -> None:
    result = runner.invoke(cli.app, ["collect", "--slot", "night"])
    assert result.exit_code == 2


def test_query_override_only_for_test_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_collect(cfg: Any, slot: str, limit: int | None, classifier: object) -> CollectResult:
        seen.append(cfg.collect.search_queries)
        return CollectResult(run_id=1, status=cli.RunStatus.OK, opened=1, saved=1, failed=0)

    monkeypatch.setattr(cli, "collect_command", fake_collect)
    ok = runner.invoke(cli.app, ["collect", "--slot", "test", "--no-ai", "--query", "игры"])
    bad = runner.invoke(cli.app, ["collect", "--slot", "morning", "--no-ai", "--query", "игры"])
    assert (ok.exit_code, bad.exit_code) == (0, 2)
    assert seen == [["игры"]]


def test_report_google_error_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_token(*_args: object) -> object:
        raise cli.GoogleAuthError("нет токена Google: python -m ttmon google-login")

    monkeypatch.setattr(cli, "report_command", no_token)
    result = runner.invoke(cli.app, ["report", "--date", "2026-10-16"])
    assert result.exit_code == cli.EXIT_GOOGLE
    assert "google-login" in result.output


def test_report_passes_date_and_test_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, bool]] = []

    def fake_report(_cfg: object, _settings: object, day: object, test: bool) -> ReportResult:
        calls.append((day, test))
        return ReportResult("https://docs.google.com/spreadsheets/d/x/edit", {"test": 3}, 3, 0)

    monkeypatch.setattr(cli, "report_command", fake_report)
    result = runner.invoke(cli.app, ["report", "--date", "2026-10-16", "--test"])
    assert result.exit_code == 0
    assert calls == [(dt.date(2026, 10, 16), True)]
    assert "spreadsheets/d/x" in result.output


@pytest.mark.parametrize(("flags", "uploads"), [([], 1), (["--no-upload"], 0)])
def test_collect_uploads_after_slot(
    monkeypatch: pytest.MonkeyPatch, flags: list[str], uploads: int
) -> None:
    uploaded: list[tuple[str, int]] = []
    monkeypatch.setattr(cli, "_classifier", lambda _cfg: object())
    monkeypatch.setattr(
        cli,
        "collect_command",
        lambda *_a: CollectResult(
            run_id=7, status=cli.RunStatus.OK, opened=25, saved=24, failed=0, selling=21
        ),
    )
    monkeypatch.setattr(
        cli, "upload_after_slot", lambda _c, _s, slot, run_id: uploaded.append((slot, run_id))
    )
    result = runner.invoke(cli.app, ["collect", "--slot", "morning", *flags])
    assert result.exit_code == 0
    assert uploaded == [("morning", 7)] * uploads


def test_process_name_valid_for_log_file() -> None:
    assert PROCESS_NAME.fullmatch(cli.process_name("google-login"))
    assert cli.process_name(None) == "cli"


def test_report_api_error_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def api_error(*_args: object) -> object:
        raise HttpError(httplib2.Response({"status": 403}), b"forbidden")

    monkeypatch.setattr(cli, "report_command", api_error)
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == cli.EXIT_GOOGLE
    assert "HttpError" in result.output


def test_serve_migrates_and_starts_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    import ttmon.db.migrate
    import ttmon.web.app

    base = load_config()
    cfg = base.model_copy(
        update={
            "paths": base.paths.model_copy(
                update={"db_file": tmp_path / "ttmon.db", "data_dir": tmp_path / "data"}
            )
        }
    )
    migrated: list[Path] = []
    served: list[tuple[str, int]] = []
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(ttmon.db.migrate, "upgrade_to_head", migrated.append)
    monkeypatch.setattr(ttmon.web.app, "create_app", lambda *_a, **_k: "app")
    monkeypatch.setattr(uvicorn, "run", lambda _app, host, port, **_k: served.append((host, port)))

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0
    assert migrated == [tmp_path / "ttmon.db"]
    assert served == [(cfg.web.host, cfg.web.port)]
