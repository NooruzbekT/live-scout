from pathlib import Path
from typing import Any

import structlog
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

log = structlog.get_logger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
JPEG_MIME = "image/jpeg"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# execute(num_retries): googleapiclient сам повторяет 5xx, 429, 403 rateLimitExceeded и сетевые сбои
RETRIES = 4


def file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Drive:
    """Папки ищутся по имени, без кэша в БД: scope drive.file видит только файлы приложения,
    а удалённая вручную папка просто создаётся заново."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._folders: dict[tuple[str | None, str], str] = {}

    def find(self, name: str, parent_id: str | None, mime_type: str) -> str | None:
        parent = _quoted(parent_id) if parent_id else "'root'"
        query = (
            f"name = {_quoted(name)} and mimeType = {_quoted(mime_type)}"
            f" and {parent} in parents and trashed = false"
        )
        response = (
            self._service.files()
            # повтор create после потерянного ответа может дать дубль: все берут самый старый
            .list(q=query, spaces="drive", fields="files(id)", orderBy="createdTime", pageSize=10)
            .execute(num_retries=RETRIES)
        )
        files = response.get("files", [])
        return str(files[0]["id"]) if files else None

    def folder(self, name: str, parent_id: str | None = None, *, public: bool = False) -> str:
        """public — доступ «все по ссылке: читатель», вложенные файлы его наследуют.
        Проверяется раз за процесс и у найденной папки: упавшая после создания папки выдача
        иначе оставила бы ссылки закрытыми навсегда."""
        key = (parent_id, name)
        if key in self._folders:
            return self._folders[key]
        folder_id = self.find(name, parent_id, FOLDER_MIME)
        if folder_id is None:
            body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
            if parent_id:
                body["parents"] = [parent_id]
            folder_id = self._create(body)
            log.info("drive_folder_created", name=name)
        if public:
            self.share_public(folder_id)
        self._folders[key] = folder_id
        return folder_id

    def upload_jpeg(self, path: Path, name: str, parent_id: str) -> str:
        """Файл с тем же именем в папке — уже загружен прошлым запуском, упавшим до записи в БД."""
        existing = self.find(name, parent_id, JPEG_MIME)
        if existing:
            return existing
        media = MediaFileUpload(str(path), mimetype=JPEG_MIME, resumable=False)
        return self._create({"name": name, "parents": [parent_id]}, media)

    def upload_as_sheet(self, path: Path, name: str, parent_id: str) -> str:
        media = MediaFileUpload(str(path), mimetype=XLSX_MIME, resumable=False)
        return self._create({"name": name, "mimeType": SHEET_MIME, "parents": [parent_id]}, media)

    def create_sheet(self, name: str, parent_id: str) -> str:
        return self._create({"name": name, "mimeType": SHEET_MIME, "parents": [parent_id]})

    def copy(self, file_id: str, name: str, parent_id: str) -> str:
        response = (
            self._service.files()
            .copy(fileId=file_id, body={"name": name, "parents": [parent_id]}, fields="id")
            .execute(num_retries=RETRIES)
        )
        return str(response["id"])

    def exists(self, file_id: str) -> bool:
        try:
            response = (
                self._service.files()
                .get(fileId=file_id, fields="id,trashed")
                .execute(num_retries=RETRIES)
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return False
            raise
        return not response.get("trashed", False)

    def share_public(self, file_id: str) -> None:
        response = (
            self._service.permissions()
            .list(fileId=file_id, fields="permissions(type,role)")
            .execute(num_retries=RETRIES)
        )
        if any(p.get("type") == "anyone" for p in response.get("permissions", [])):
            return
        self._permission(file_id, {"type": "anyone", "role": "reader"})
        log.info("drive_shared_by_link")

    def share_with(self, file_id: str, emails: list[str]) -> None:
        for email in emails:
            self._permission(
                file_id,
                {"type": "user", "role": "reader", "emailAddress": email},
                sendNotificationEmail=False,
            )

    def _permission(self, file_id: str, body: dict[str, str], **kwargs: Any) -> None:
        self._service.permissions().create(
            fileId=file_id, body=body, fields="id", **kwargs
        ).execute(num_retries=RETRIES)

    def _create(self, body: dict[str, Any], media: MediaFileUpload | None = None) -> str:
        response = (
            self._service.files()
            .create(body=body, media_body=media, fields="id")
            .execute(num_retries=RETRIES)
        )
        return str(response["id"])
