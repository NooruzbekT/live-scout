from dataclasses import dataclass
from typing import Any

from ttmon.google.drive import RETRIES

COLUMNS = 5  # A–E: дата, №, категория, скриншот, цены
CELL_FIELDS = "userEnteredValue,textFormatRuns"


@dataclass(frozen=True)
class Link:
    """Ссылка rich-text, а не формула HYPERLINK: у формулы разделитель аргументов зависит
    от локали таблицы (`;` для ru, `,` для en)."""

    text: str
    url: str


type Cell = str | int | Link


class SheetsError(RuntimeError):
    pass


def cell_data(value: Cell) -> dict[str, Any]:
    if isinstance(value, Link):
        return {
            "userEnteredValue": {"stringValue": value.text},
            "textFormatRuns": [{"startIndex": 0, "format": {"link": {"uri": value.url}}}],
        }
    if isinstance(value, int):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": value}}


def spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


class Sheets:
    def __init__(self, service: Any) -> None:
        self._service = service

    def grid(self, spreadsheet_id: str) -> dict[str, tuple[int, int]]:
        """Лист → (sheetId, число строк)."""
        response = (
            self._service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties(sheetId,title,gridProperties.rowCount)",
            )
            .execute(num_retries=RETRIES)
        )
        return {
            s["properties"]["title"]: (
                s["properties"]["sheetId"],
                s["properties"].get("gridProperties", {}).get("rowCount", 0),
            )
            for s in response.get("sheets", [])
        }

    def ensure_sheets(self, spreadsheet_id: str, titles: list[str], header: list[Cell]) -> None:
        """Таблица без шаблона: листы слотов и строка заголовка. Заголовок пишется при каждом
        вызове: прерванная между двумя batch подготовка доделывается при следующем отчёте."""
        grid = self.grid(spreadsheet_id)
        missing = [t for t in titles if t not in grid]
        if missing:
            self._add_sheets(spreadsheet_id, grid, titles, missing)
            grid = self.grid(spreadsheet_id)
        self._batch(
            spreadsheet_id,
            [
                {
                    "updateCells": {
                        "start": {"sheetId": grid[t][0], "rowIndex": 0, "columnIndex": 0},
                        "rows": [{"values": [cell_data(v) for v in header]}],
                        "fields": CELL_FIELDS,
                    }
                }
                for t in titles
            ],
        )

    def _add_sheets(
        self,
        spreadsheet_id: str,
        grid: dict[str, tuple[int, int]],
        titles: list[str],
        missing: list[str],
    ) -> None:
        requests: list[dict[str, Any]] = []
        spare = [sheet_id for title, (sheet_id, _) in grid.items() if title not in titles]
        if spare:
            # пустая таблица Drive создаётся с листом «Лист1»/«Sheet1» — переименовываем его
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": spare[0], "title": missing[0]},
                        "fields": "title",
                    }
                }
            )
            requests += [{"addSheet": {"properties": {"title": t}}} for t in missing[1:]]
        else:
            requests += [{"addSheet": {"properties": {"title": t}}} for t in missing]
        self._batch(spreadsheet_id, requests)

    def write(
        self, spreadsheet_id: str, data: dict[str, list[list[Cell]]], header_rows: int
    ) -> None:
        """Всё ниже заголовка в A–E перезаписывается: значения строк-примеров шаблона и прошлого
        прогона за ту же дату стираются (поля CELL_FIELDS вне rows в диапазоне updateCells
        очищаются). Оформление первой строки данных шаблона (рамки, шрифт, выравнивание)
        копируется на все строки отчёта, ниже «Итого» — снимается; высота строк по содержимому."""
        grid = self.grid(spreadsheet_id)
        missing = [title for title in data if title not in grid]
        if missing:
            raise SheetsError(f"в таблице нет листов: {', '.join(missing)}")
        requests: list[dict[str, Any]] = []
        for title, rows in data.items():
            sheet_id, row_count = grid[title]
            needed = header_rows + len(rows)
            if needed > row_count:
                requests.append(
                    {
                        "appendDimension": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "length": needed - row_count,
                        }
                    }
                )
                row_count = needed
            if needed > header_rows + 1:
                requests.append(
                    {
                        "copyPaste": {
                            "source": _rows(sheet_id, header_rows, header_rows + 1),
                            "destination": _rows(sheet_id, header_rows + 1, needed),
                            "pasteType": "PASTE_FORMAT",
                        }
                    }
                )
            # строка header_rows — образец оформления для copyPaste, её формат не трогаем
            keep = max(needed, header_rows + 1)
            if row_count > keep:
                # рамки и проверки данных строк-примеров шаблона и прошлого, более длинного прогона
                requests.append(
                    {
                        "updateCells": {
                            "range": _rows(sheet_id, keep, row_count),
                            "fields": "userEnteredFormat,dataValidation",
                        }
                    }
                )
            # значения — после копирования формата: PASTE_FORMAT не должен задеть ссылки
            requests.append(
                {
                    "updateCells": {
                        "range": _rows(sheet_id, header_rows, row_count),
                        "rows": [{"values": [cell_data(v) for v in row]} for row in rows],
                        "fields": CELL_FIELDS,
                    }
                }
            )
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": needed,
                        }
                    }
                }
            )
        self._batch(spreadsheet_id, requests)

    def _batch(self, spreadsheet_id: str, requests: list[dict[str, Any]]) -> None:
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute(num_retries=RETRIES)


def _rows(sheet_id: int, start: int, end: int) -> dict[str, int]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": start,
        "endRowIndex": end,
        "startColumnIndex": 0,
        "endColumnIndex": COLUMNS,
    }
