import io
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from ttmon.media.stamp import stamp_image, stamp_text
from ttmon.settings import load_config

ALMATY = ZoneInfo("Asia/Almaty")


def _jpeg(width: int = 1440, height: int = 900) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(out, "JPEG")
    return out.getvalue()


def test_stamp_text() -> None:
    moment = datetime(2026, 10, 16, 10, 7, tzinfo=ALMATY)
    expected = "16.10.2026 10:07 · Алматы · @shop.almaty.test"
    assert stamp_text(moment, "Алматы", "shop.almaty.test") == expected


@pytest.mark.parametrize("font_ok", [True, False])
def test_plate_in_bottom_right(tmp_path: Path, font_ok: bool) -> None:
    font = load_config().media.font_path if font_ok else tmp_path / "missing.ttf"
    result = Image.open(
        io.BytesIO(stamp_image(_jpeg(), "16.10.2026 10:07 · Алматы · @x", font, 85))
    )

    assert result.size == (1440, 900)
    corner = result.crop((1100, 820, 1430, 890)).convert("L")
    top_left = result.crop((10, 10, 340, 80)).convert("L")
    assert min(corner.tobytes()) < 100
    assert min(top_left.tobytes()) > 150
