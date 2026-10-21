from pathlib import Path

from ttmon.media.layout import ai_input_path


def test_ai_input_mirrors_screenshot_path() -> None:
    shot = Path("data/test/screenshots/2026-10-16/test/07_shop_1007.jpg")
    assert ai_input_path(shot) == Path("data/test/ai_inputs/2026-10-16/test/07_shop_1007.jpg")


def test_outside_screenshots_dir() -> None:
    assert ai_input_path(Path("data/other/x.jpg")) is None
