"""Подставные сервисы Drive v3 и Sheets v4 в памяти: только то, что вызывает ttmon."""

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

from ttmon.google.drive import FOLDER_MIME, SHEET_MIME

QUERY = re.compile(
    r"name = '((?:[^'\\]|\\.)*)' and mimeType = '([^']*)'"
    r" and '([^']*)' in parents and trashed = false"
)


class Request:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self, num_retries: int = 0) -> Any:
        return self._fn()


@dataclass
class FakeFile:
    name: str
    mime_type: str
    parents: list[str]
    trashed: bool = False


@dataclass
class FakeSheet:
    sheet_id: int
    title: str
    row_count: int = 1000
    cells: dict[tuple[int, int], Any] = field(default_factory=dict)
    # оформление строки целиком (метка из шаблона): достаточно, чтобы проверить копирование/очистку
    formats: dict[int, str] = field(default_factory=dict)
    resized: list[tuple[int, int]] = field(default_factory=list)

    def row(self, index: int) -> list[Any]:
        width = max((c for r, c in self.cells if r == index), default=-1) + 1
        return [self.cells.get((index, c)) for c in range(width)]


class FakeDriveService:
    def __init__(self) -> None:
        self.files_by_id: dict[str, FakeFile] = {}
        self.permissions_created: list[tuple[str, dict[str, str]]] = []
        self.uploads = 0
        self._ids = itertools.count(1)
        self.spreadsheets: dict[str, list[FakeSheet]] = {}

    def files(self) -> "FakeFiles":
        return FakeFiles(self)

    def permissions(self) -> "FakePermissions":
        return FakePermissions(self)

    def _add(self, name: str, mime: str, parents: list[str], source: str | None = None) -> str:
        file_id = f"id{next(self._ids)}"
        self.files_by_id[file_id] = FakeFile(name, mime, parents)
        if mime == SHEET_MIME:
            sheets = self.spreadsheets.get(source or "", [FakeSheet(0, "Лист1")])
            self.spreadsheets[file_id] = [
                FakeSheet(s.sheet_id, s.title, s.row_count, dict(s.cells), dict(s.formats))
                for s in sheets
            ]
        return file_id

    def by_name(self, name: str, mime: str = FOLDER_MIME) -> list[str]:
        return [i for i, f in self.files_by_id.items() if f.name == name and f.mime_type == mime]


class FakeFiles:
    def __init__(self, drive: FakeDriveService) -> None:
        self._drive = drive

    def list(self, q: str, **_: Any) -> Request:
        match = QUERY.fullmatch(q)
        assert match, q
        name = re.sub(r"\\(.)", r"\1", match.group(1))
        mime, parent = match.group(2), match.group(3)

        def run() -> dict[str, Any]:
            found = [
                {"id": file_id}
                for file_id, f in self._drive.files_by_id.items()
                if f.name == name and f.mime_type == mime and parent in f.parents and not f.trashed
            ]
            return {"files": found}

        return Request(run)

    def create(self, body: dict[str, Any], media_body: Any = None, **_: Any) -> Request:
        def run() -> dict[str, str]:
            if media_body is not None and body.get("mimeType") != SHEET_MIME:
                self._drive.uploads += 1
            mime = body.get("mimeType") or (media_body.mimetype() if media_body else "")
            return {"id": self._drive._add(body["name"], mime, body.get("parents", ["root"]))}

        return Request(run)

    def copy(self, fileId: str, body: dict[str, Any], **_: Any) -> Request:  # noqa: N803
        def run() -> dict[str, str]:
            source = self._drive.files_by_id[fileId]
            return {"id": self._drive._add(body["name"], source.mime_type, body["parents"], fileId)}

        return Request(run)

    def get(self, fileId: str, **_: Any) -> Request:  # noqa: N803
        def run() -> dict[str, Any]:
            if fileId not in self._drive.files_by_id:
                raise HttpError(httplib2.Response({"status": 404}), b"not found")
            return {"id": fileId, "trashed": self._drive.files_by_id[fileId].trashed}

        return Request(run)


class FakePermissions:
    def __init__(self, drive: FakeDriveService) -> None:
        self._drive = drive

    def list(self, fileId: str, **_: Any) -> Request:  # noqa: N803
        def run() -> dict[str, Any]:
            found = [body for file_id, body in self._drive.permissions_created if file_id == fileId]
            return {"permissions": found}

        return Request(run)

    def create(self, fileId: str, body: dict[str, str], **_: Any) -> Request:  # noqa: N803
        def run() -> dict[str, str]:
            self._drive.permissions_created.append((fileId, body))
            return {"id": "perm"}

        return Request(run)


def _value(cell: dict[str, Any]) -> Any:
    entered = cell.get("userEnteredValue", {})
    value = entered.get("stringValue", entered.get("numberValue"))
    runs = cell.get("textFormatRuns")
    if runs:
        return (value, runs[0]["format"]["link"]["uri"])
    return value


class FakeSheetsService:
    """Таблицы живут в FakeDriveService.spreadsheets: копия шаблона копирует и листы."""

    def __init__(self, drive: FakeDriveService) -> None:
        self._drive = drive
        self.batches = 0

    def spreadsheets(self) -> "FakeSheetsService":
        return self

    def get(self, spreadsheetId: str, **_: Any) -> Request:  # noqa: N803
        def run() -> dict[str, Any]:
            return {
                "sheets": [
                    {
                        "properties": {
                            "sheetId": s.sheet_id,
                            "title": s.title,
                            "gridProperties": {"rowCount": s.row_count},
                        }
                    }
                    for s in self._drive.spreadsheets[spreadsheetId]
                ]
            }

        return Request(run)

    def batchUpdate(self, spreadsheetId: str, body: dict[str, Any]) -> Request:  # noqa: N802, N803
        def run() -> dict[str, Any]:
            self.batches += 1
            sheets = self._drive.spreadsheets[spreadsheetId]
            by_id = {s.sheet_id: s for s in sheets}
            for request in body["requests"]:
                if "addSheet" in request:
                    new = FakeSheet(max(by_id) + 1, request["addSheet"]["properties"]["title"])
                    sheets.append(new)
                    by_id[new.sheet_id] = new
                elif "updateSheetProperties" in request:
                    props = request["updateSheetProperties"]["properties"]
                    by_id[props["sheetId"]].title = props["title"]
                elif "appendDimension" in request:
                    dim = request["appendDimension"]
                    by_id[dim["sheetId"]].row_count += dim["length"]
                elif "updateCells" in request:
                    self._update_cells(by_id, request["updateCells"])
                elif "copyPaste" in request:
                    paste = request["copyPaste"]
                    assert paste["pasteType"] == "PASTE_FORMAT"
                    src, dst = paste["source"], paste["destination"]
                    sheet = by_id[src["sheetId"]]
                    assert dst["endRowIndex"] <= sheet.row_count, "вставка за пределами листа"
                    fmt = sheet.formats.get(src["startRowIndex"])
                    for r in range(dst["startRowIndex"], dst["endRowIndex"]):
                        if fmt is None:
                            sheet.formats.pop(r, None)
                        else:
                            sheet.formats[r] = fmt
                elif "autoResizeDimensions" in request:
                    dims = request["autoResizeDimensions"]["dimensions"]
                    by_id[dims["sheetId"]].resized.append((dims["startIndex"], dims["endIndex"]))
            return {}

        return Request(run)

    @staticmethod
    def _update_cells(by_id: dict[int, FakeSheet], update: dict[str, Any]) -> None:
        fields = update["fields"].split(",")
        if "range" in update:
            rng = update["range"]
            sheet, top, left = by_id[rng["sheetId"]], rng["startRowIndex"], rng["startColumnIndex"]
            assert rng["endRowIndex"] <= sheet.row_count, "диапазон за пределами листа"
            for r in range(top, rng["endRowIndex"]):
                if "userEnteredFormat" in fields:
                    sheet.formats.pop(r, None)
                if "userEnteredValue" in fields:
                    for c in range(left, rng["endColumnIndex"]):
                        sheet.cells.pop((r, c), None)
        else:
            start = update["start"]
            sheet, top, left = by_id[start["sheetId"]], start["rowIndex"], start["columnIndex"]
        for r, row in enumerate(update.get("rows", [])):
            for c, cell in enumerate(row["values"]):
                sheet.cells[(top + r, left + c)] = _value(cell)
