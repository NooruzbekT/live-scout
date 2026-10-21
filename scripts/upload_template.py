"""xlsx-шаблон отчёта → Google Таблица в корневой папке Drive, её ID → .env
(GOOGLE_TEMPLATE_SHEET_ID).

python scripts/upload_template.py путь/к/шаблону.xlsx
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ttmon.google.auth import services  # noqa: E402
from ttmon.settings import PROJECT_ROOT, get_config, get_settings  # noqa: E402

TEMPLATE_NAME = "Шаблон отчёта TT эфиры"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_KEY = "GOOGLE_TEMPLATE_SHEET_ID"
ENV_LINE = re.compile(rf"^{ENV_KEY}=.*$", re.MULTILINE)


def main() -> int:
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        print(__doc__)
        return 2
    cfg = get_config()
    drive, sheets = services(get_settings())
    root = drive.folder(cfg.google.drive_root_folder_name, public=True)
    sheet_id = drive.upload_as_sheet(Path(sys.argv[1]), TEMPLATE_NAME, root)

    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.is_file() else ""
    line = f"{ENV_KEY}={sheet_id}"
    if ENV_LINE.search(text):
        text = ENV_LINE.sub(line, text)
    else:
        text = text + ("\n" if text and not text.endswith("\n") else "") + line + "\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    print(f"ID шаблона записан в {ENV_FILE} ({ENV_KEY})")

    titles = list(sheets.grid(sheet_id))
    print(f"Шаблон: https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
    print(f"Листы: {', '.join(titles)}")
    missing = [s.sheet for s in cfg.slots.values() if s.sheet not in titles]
    if missing:
        print(f"В шаблоне нет листов из config.yaml slots.*.sheet: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
