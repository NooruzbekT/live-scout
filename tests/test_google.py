from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.auth.exceptions import RefreshError

from tests.google_fakes import FakeDriveService, FakeSheetsService
from ttmon.google import auth
from ttmon.google.auth import GoogleAuthError, load_credentials, login
from ttmon.google.drive import FOLDER_MIME, JPEG_MIME, Drive
from ttmon.google.sheets import Link, Sheets, SheetsError
from ttmon.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        google_credentials_file=tmp_path / "credentials.json",
        google_token_file=tmp_path / "token.json",
    )


def test_folder_created_once_and_found_by_new_process() -> None:
    service = FakeDriveService()

    root = Drive(service).folder("TT эфиры", public=True)
    again = Drive(service).folder("TT эфиры", public=True)
    child = Drive(service).folder("2026-10-21", root)

    assert root == again
    assert service.by_name("TT эфиры") == [root]
    assert service.files_by_id[child].parents == [root]
    # доступ по ссылке — только корню, вложенные наследуют; повторно не выдаётся
    assert service.permissions_created == [(root, {"type": "anyone", "role": "reader"})]


def test_query_escapes_quotes() -> None:
    service = FakeDriveService()
    drive = Drive(service)
    first = drive.folder("магазин 'Айгерим'")
    assert Drive(service).folder("магазин 'Айгерим'") == first


def test_trashed_folder_recreated() -> None:
    service = FakeDriveService()
    old = Drive(service).folder("TT эфиры")
    service.files_by_id[old].trashed = True
    assert Drive(service).folder("TT эфиры") != old


def test_upload_reuses_file_with_same_name(tmp_path: Path) -> None:
    shot = tmp_path / "01_shop_1007.jpg"
    shot.write_bytes(b"\xff\xd8jpeg")
    service = FakeDriveService()
    folder = Drive(service).folder("Утро")

    first = Drive(service).upload_jpeg(shot, shot.name, folder)
    second = Drive(service).upload_jpeg(shot, shot.name, folder)

    assert first == second
    assert service.uploads == 1
    assert service.files_by_id[first].mime_type == JPEG_MIME


def test_exists() -> None:
    service = FakeDriveService()
    folder = Drive(service).folder("x")
    assert Drive(service).exists(folder)
    assert not Drive(service).exists("missing")
    service.files_by_id[folder].trashed = True
    assert not Drive(service).exists(folder)


def test_write_clears_old_rows_and_grows_sheet() -> None:
    drive = FakeDriveService()
    sheet_id = Drive(drive).create_sheet("Отчёт", Drive(drive).folder("TT эфиры"))
    sheets = Sheets(FakeSheetsService(drive))
    sheets.ensure_sheets(sheet_id, ["Утро", "Обед"], ["Дата", "№"])
    tab = drive.spreadsheets[sheet_id][0]
    tab.row_count = 3

    sheets.write(sheet_id, {"Утро": [["a", 1], ["b", 2], ["c", 3], ["Итого:", 3]]}, 1)
    assert tab.row_count == 5
    sheets.write(sheet_id, {"Утро": [["x", 1, Link("Скриншот 10:07", "https://d/1")]]}, 1)

    assert tab.row(0) == ["Дата", "№"]
    assert tab.row(1) == ["x", 1, ("Скриншот 10:07", "https://d/1")]
    assert tab.row(2) == [] and tab.row(4) == []


def test_write_rejects_missing_sheet() -> None:
    drive = FakeDriveService()
    sheet_id = Drive(drive).create_sheet("Отчёт", Drive(drive).folder("TT эфиры"))
    with pytest.raises(SheetsError, match="Вечер"):
        Sheets(FakeSheetsService(drive)).write(sheet_id, {"Вечер": []}, 1)


def test_load_credentials_without_token(tmp_path: Path) -> None:
    with pytest.raises(GoogleAuthError, match="google-login"):
        load_credentials(_settings(tmp_path))


def test_login_without_client_file(tmp_path: Path) -> None:
    with pytest.raises(GoogleAuthError, match="credentials.json"):
        login(_settings(tmp_path))


def test_revoked_token_asks_relogin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    settings.google_token_file.write_text("{}", encoding="utf-8")

    def refresh(_request: Any) -> None:
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    creds = SimpleNamespace(valid=False, refresh_token="r", token=None, refresh=refresh)
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", lambda *_a, **_k: creds)
    with pytest.raises(GoogleAuthError, match="invalid_grant"):
        load_credentials(settings)


def test_folder_mime_used_for_lookup() -> None:
    service = FakeDriveService()
    Drive(service).create_sheet("TT эфиры", Drive(service).folder("root-like"))
    assert service.by_name("TT эфиры", FOLDER_MIME) == []


def test_existing_root_without_link_access_gets_shared() -> None:
    service = FakeDriveService()
    root = Drive(service).folder("TT эфиры")
    assert service.permissions_created == []

    Drive(service).folder("TT эфиры", public=True)

    assert service.permissions_created == [(root, {"type": "anyone", "role": "reader"})]


def test_ensure_sheets_finishes_interrupted_setup() -> None:
    drive = FakeDriveService()
    sheet_id = Drive(drive).create_sheet("Отчёт", Drive(drive).folder("TT эфиры"))
    sheets = Sheets(FakeSheetsService(drive))
    sheets.ensure_sheets(sheet_id, ["Утро"], ["Дата"])

    sheets.ensure_sheets(sheet_id, ["Утро", "Обед", "Вечер"], ["Дата"])
    sheets.ensure_sheets(sheet_id, ["Утро", "Обед", "Вечер"], ["Дата"])

    tabs = drive.spreadsheets[sheet_id]
    assert [t.title for t in tabs] == ["Утро", "Обед", "Вечер"]
    assert all(t.row(0) == ["Дата"] for t in tabs)


def test_broken_token_asks_relogin(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.google_token_file.write_text("{не json", encoding="utf-8")
    with pytest.raises(GoogleAuthError, match="повреждён"):
        load_credentials(settings)
