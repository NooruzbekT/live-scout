import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Annotated, NoReturn

import structlog
import typer

from ttmon.ai import evaluate
from ttmon.ai.base import Classifier
from ttmon.ai.factory import AIConfigError, build_classifier
from ttmon.ai.gemini import AIUnavailable
from ttmon.collector.browser import BrowserLaunchError, LoginState, check_login, interactive_login
from ttmon.collector.run import collect_command, collect_lock_path
from ttmon.db.models import RunStatus
from ttmon.google.auth import GoogleAuthError
from ttmon.google.auth import login as google_login_flow
from ttmon.lock import AlreadyRunning, exclusive_lock
from ttmon.logging import setup_logging
from ttmon.report.commands import GOOGLE_ERRORS, error_text, report_command, upload_after_slot
from ttmon.settings import AppConfig, get_config, get_settings, project_path
from ttmon.timeutil import today_almaty

log = structlog.get_logger(__name__)

# 1 — необработанное исключение, 2 — ошибка аргументов click
EXIT_NOT_IMPLEMENTED = 3
EXIT_LOGGED_OUT = 4
EXIT_LOGIN_UNKNOWN = 5
EXIT_BROWSER_FAILED = 6
EXIT_PARTIAL = 7
EXIT_FAILED = 8
EXIT_ALREADY_RUNNING = 9
EXIT_AI_CONFIG = 10
EXIT_EVAL_FAILED = 11
EXIT_GOOGLE = 12

COLLECT_EXIT_CODES = {
    RunStatus.OK: 0,
    RunStatus.PARTIAL: EXIT_PARTIAL,
    RunStatus.FAILED: EXIT_FAILED,
    RunStatus.ABORTED: EXIT_FAILED,
    RunStatus.RUNNING: EXIT_FAILED,
}

LOGIN_EXIT_CODES = {
    LoginState.LOGGED_IN: 0,
    LoginState.LOGGED_OUT: EXIT_LOGGED_OUT,
    LoginState.UNKNOWN: EXIT_LOGIN_UNKNOWN,
}
LOGIN_MESSAGES = {
    LoginState.LOGGED_IN: "TikTok: залогинен.",
    LoginState.LOGGED_OUT: "TikTok: не залогинен. Выполните: python -m ttmon login",
    LoginState.UNKNOWN: "TikTok: не удалось проверить (403, капча или страница не загрузилась). "
    "Подождите 10-20 минут и повторите --check.",
}

app = typer.Typer(
    help="TT Live Monitor: сбор продающих TikTok LIVE эфиров и отчёт.",
    no_args_is_help=True,
    add_completion=False,
)


class SlotArg(StrEnum):
    morning = "morning"
    noon = "noon"
    evening = "evening"
    test = "test"


def _not_implemented(stage: int) -> NoReturn:
    typer.echo(f"не реализовано (этап {stage})", err=True)
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


def process_name(command: str | None) -> str:
    # имя процесса идёт в имя файла лога: допустимы [a-z0-9_] (logging.PROCESS_NAME)
    return (command or "cli").replace("-", "_")


@app.callback()
def main(ctx: typer.Context) -> None:
    config = get_config()
    logs_dir = project_path(config.paths.logs_dir)
    setup_logging(logs_dir, process_name(ctx.invoked_subcommand), config.tz)


@app.command()
def serve() -> None:
    """Веб-панель и планировщик: слоты и отчёт по расписанию (Ctrl+C — остановить)."""
    import uvicorn

    from ttmon.db.migrate import upgrade_to_head
    from ttmon.media.layout import SCREENSHOTS_DIR
    from ttmon.web.app import create_app

    config = get_config()
    db_file = project_path(config.paths.db_file)
    upgrade_to_head(db_file)
    screenshots = project_path(config.paths.data_dir) / SCREENSHOTS_DIR
    web = create_app(config, db_file, screenshots)
    typer.echo(f"Панель: http://{config.web.host}:{config.web.port}")
    # log_config=None: логи идут через structlog (файл serve), access-лог не нужен
    uvicorn.run(web, host=config.web.host, port=config.web.port, log_config=None, access_log=False)


@app.command()
def login(
    check: Annotated[bool, typer.Option("--check", help="Только проверить вход.")] = False,
) -> None:
    """Войти в TikTok в профиле браузера сервиса (окно закрыть после входа)."""
    config = get_config()
    try:
        with exclusive_lock(collect_lock_path(config)):
            if not check:
                typer.echo(
                    "Войдите в рабочий аккаунт TikTok в открывшемся окне Chrome и закройте окно. "
                    "Другие окна с этим профилем должны быть закрыты."
                )
                interactive_login(config)
            state = asyncio.run(check_login(config))
    except AlreadyRunning as exc:
        typer.echo(f"{exc}: дождитесь окончания сбора", err=True)
        raise typer.Exit(EXIT_ALREADY_RUNNING) from exc
    except BrowserLaunchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_BROWSER_FAILED) from exc
    typer.echo(LOGIN_MESSAGES[state])
    raise typer.Exit(LOGIN_EXIT_CODES[state])


@app.command()
def collect(
    slot: Annotated[SlotArg, typer.Option(help="Слот сбора.")],
    limit: Annotated[
        int | None,
        typer.Option(
            min=1, help="Потолок открытых эфиров (отладка). По умолчанию стоп по продающим."
        ),
    ] = None,
    no_ai: Annotated[bool, typer.Option("--no-ai", help="Без AI классификации.")] = False,
    no_upload: Annotated[bool, typer.Option("--no-upload", help="Без загрузки на Drive.")] = False,
    query: Annotated[
        list[str] | None,
        typer.Option(help="Только для --slot test: свои поисковые запросы вместо config.yaml."),
    ] = None,
) -> None:
    """Собрать эфиры за слот."""
    config = get_config()
    if query:
        if slot != SlotArg.test:
            typer.echo("--query только для --slot test: боевые запросы — в config.yaml", err=True)
            raise typer.Exit(2)
        collect_cfg = config.collect.model_copy(update={"search_queries": query})
        config = config.model_copy(update={"collect": collect_cfg})
    classifier = None if no_ai else _classifier(config)
    try:
        result = collect_command(config, slot.value, limit, classifier)
    except AlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_ALREADY_RUNNING) from exc
    except BrowserLaunchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_BROWSER_FAILED) from exc
    if not no_ai and not no_upload and result.selling:
        upload_after_slot(config, get_settings(), slot.value, result.run_id)
    selling = "" if no_ai else f", продающих {result.selling}"
    typer.echo(
        f"{slot.value}: {result.status}, открыто {result.opened}, сохранено {result.saved}"
        f"{selling}, ошибок {result.failed}"
    )
    raise typer.Exit(COLLECT_EXIT_CODES[result.status])


def _classifier(config: AppConfig) -> Classifier:
    try:
        return build_classifier(config, get_settings())
    except AIConfigError as exc:
        typer.echo(f"AI: {exc}", err=True)
        raise typer.Exit(EXIT_AI_CONFIG) from exc


@app.command()
def report(
    date: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Дата отчёта, по умолчанию сегодня."),
    ] = None,
    test: Annotated[
        bool, typer.Option("--test", help="Отчёт по тестовой БД (collect --slot test).")
    ] = False,
) -> None:
    """Собрать отчёт за день в Google Таблицу."""
    config = get_config()
    day = date.date() if date else today_almaty(config.tz)
    try:
        result = report_command(config, get_settings(), day, test)
    except GOOGLE_ERRORS as exc:
        message = error_text(exc)
        log.error("report_failed", error=message)
        typer.echo(message, err=True)
        raise typer.Exit(EXIT_GOOGLE) from exc
    rows = ", ".join(f"{slot}: {count}" for slot, count in result.rows.items())
    failed = f", не загружено {result.upload_failed}" if result.upload_failed else ""
    typer.echo(f"Отчёт: {result.url}")
    typer.echo(f"Строк: {rows}; загружено скриншотов {result.uploaded}{failed}")
    raise typer.Exit(EXIT_GOOGLE if result.upload_failed else 0)


@app.command("google-login")
def google_login() -> None:
    """Войти в Google (Drive, Sheets): откроется браузер, токен сохранится в secrets/."""
    try:
        google_login_flow(get_settings())
    except GoogleAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_GOOGLE) from exc
    typer.echo(f"Google: вход выполнен, токен в {get_settings().google_token_file}")


@app.command("eval")
def eval_(
    prepare_count: Annotated[
        int | None,
        typer.Option(
            "--prepare", min=1, help="Добавить N скриншотов из собранных эфиров в набор разметки."
        ),
    ] = None,
) -> None:
    """Прогнать классификатор по размеченным скриншотам (evaluation.dir/labels.yaml)."""
    config = get_config()
    eval_dir = project_path(config.evaluation.dir)
    if prepare_count is not None:
        sources = [
            project_path(config.paths.test_data_dir) / "ttmon.db",
            project_path(config.paths.db_file),
        ]
        added = evaluate.prepare(eval_dir, sources, prepare_count)
        typer.echo(f"Добавлено {added}. Разметьте label в {eval_dir / evaluate.LABELS_FILE}")
        return
    labeled = [i for i in evaluate.load_items(eval_dir) if i.label.is_selling is not None]
    if not labeled:
        typer.echo(f"Нет размеченных скриншотов в {eval_dir}. Начните с --prepare 50", err=True)
        raise typer.Exit(EXIT_EVAL_FAILED)
    try:
        metrics, _ = asyncio.run(evaluate.run_eval(_classifier(config), config, eval_dir))
    except AIUnavailable as exc:
        typer.echo(f"AI недоступен: {exc}", err=True)
        raise typer.Exit(EXIT_AI_CONFIG) from exc
    thresholds = config.evaluation

    def line(name: str, value: float | None, limit: float, detail: str) -> str:
        shown = "—" if value is None else f"{value:.0%}"
        mark = "ok" if value is not None and value >= limit else "НИЖЕ ПОРОГА"
        return f"{name:<22} {shown:>5}  (порог {limit:.0%}, {detail})  {mark}"

    typer.echo(f"Эфиров: {metrics.selling_total}, невалидных ответов: {metrics.invalid_answers}")
    typer.echo(
        line(
            "продающий/нет",
            metrics.selling_accuracy,
            thresholds.selling_accuracy,
            f"{metrics.selling_correct}/{metrics.selling_total}",
        )
    )
    typer.echo(
        line(
            "категория",
            metrics.category_accuracy,
            thresholds.category_accuracy,
            f"{metrics.category_correct}/{metrics.category_total}",
        )
    )
    typer.echo(
        line(
            "цены с экрана",
            metrics.price_recall,
            thresholds.price_recall,
            f"{metrics.prices_found}/{metrics.prices_total}",
        )
    )
    typer.echo(f"Токены: вход {metrics.prompt_tokens}, выход {metrics.output_tokens}")
    typer.echo(f"Подробно: {eval_dir / evaluate.RESULTS_FILE}")
    raise typer.Exit(0 if metrics.passed(thresholds) else EXIT_EVAL_FAILED)


@app.command()
def doctor() -> None:
    """Проверить окружение: Chrome, логин TikTok, ключи, Google, Telegram."""
    _not_implemented(9)


if __name__ == "__main__":
    app()
