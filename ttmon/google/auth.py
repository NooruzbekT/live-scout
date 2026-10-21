from pathlib import Path
from typing import Any

import structlog
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ttmon.google.drive import Drive
from ttmon.google.sheets import Sheets
from ttmon.logging import register_secret
from ttmon.settings import Settings

log = structlog.get_logger(__name__)

# drive.file — только файлы, созданные приложением; Sheets API с ним работает с теми
# же файлами, а scope spreadsheets открыл бы все таблицы аккаунта. Шаблон грузит само приложение
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
RELOGIN = "python -m ttmon google-login"


class GoogleAuthError(RuntimeError):
    pass


def _save(creds: Credentials, token_file: Path) -> None:
    # через временный файл: обрыв посреди записи не оставляет битый token.json
    token_file.parent.mkdir(parents=True, exist_ok=True)
    partial = token_file.with_suffix(".tmp")
    partial.write_text(creds.to_json(), encoding="utf-8")
    partial.replace(token_file)


def _register(creds: Credentials) -> None:
    for value in (creds.token, creds.refresh_token):
        if value:
            register_secret(value)


def login(settings: Settings) -> Credentials:
    client_file = settings.google_credentials_file
    if not client_file.is_file():
        raise GoogleAuthError(
            f"нет {client_file}: создайте OAuth client (Desktop app) в Google Cloud Console "
            "и сохраните его JSON по этому пути"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    creds: Credentials = flow.run_local_server(port=0, open_browser=True)
    if not creds.refresh_token:
        raise GoogleAuthError("Google не выдал refresh_token: отзовите доступ и войдите заново")
    _register(creds)
    _save(creds, settings.google_token_file)
    return creds


def load_credentials(settings: Settings) -> Credentials:
    token_file = settings.google_token_file
    if not token_file.is_file():
        raise GoogleAuthError(f"нет токена Google ({token_file}): {RELOGIN}")
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    except ValueError as exc:
        # битый JSON или нет refresh_token/client_id (to_json опускает пустые поля)
        raise GoogleAuthError(f"токен Google повреждён ({exc}): {RELOGIN}") from exc
    _register(creds)
    if creds.valid:
        return creds
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        # invalid_grant: токен отозван или OAuth-приложение в статусе Testing (токен живёт 7 дней)
        raise GoogleAuthError(f"токен Google недействителен ({exc.args[0]}): {RELOGIN}") from exc
    _register(creds)
    _save(creds, token_file)
    log.info("google_token_refreshed")
    return creds


def services(settings: Settings) -> tuple[Drive, Sheets]:
    creds = load_credentials(settings)
    drive: Any = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets: Any = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return Drive(drive), Sheets(sheets)
