from pathlib import Path

SCREENSHOTS_DIR = "screenshots"
# кадр без штампа в том виде, в каком его видел AI: для eval и разбора ошибок классификации
# (на штампе дата и время — их можно принять за цену)
AI_INPUTS_DIR = "ai_inputs"
# звук, отправленный во второй проход AI: рядом со screenshots/; срок — AUDIO_KEEP_DAYS
AUDIO_DIR = "audio"


def ai_input_path(screenshot: Path) -> Path | None:
    """…/screenshots/2026-10-16/morning/x.jpg → …/ai_inputs/2026-10-16/morning/x.jpg."""
    parts = list(screenshot.parts)
    if SCREENSHOTS_DIR not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index(SCREENSHOTS_DIR)
    parts[index] = AI_INPUTS_DIR
    return Path(*parts)
