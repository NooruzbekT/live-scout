import io
from datetime import datetime
from pathlib import Path

import structlog
from PIL import Image, ImageDraw, ImageFont

log = structlog.get_logger(__name__)

STAMP_DATETIME_FORMAT = "%d.%m.%Y %H:%M"
# размер шрифта от высоты кадра: на 900 px ≈ 20 px
FONT_HEIGHT_RATIO = 45
MIN_FONT_PX = 14
PLATE_ALPHA = 160
MARGIN_RATIO = 0.6


def stamp_text(captured_local: datetime, city: str, username: str) -> str:
    return f"{captured_local.strftime(STAMP_DATETIME_FORMAT)} · {city} · @{username}"


def _font(font_path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(font_path), size)
    except OSError:
        log.warning("stamp_font_fallback", font_path=str(font_path))
        return ImageFont.load_default(size)


def stamp_image(image_bytes: bytes, text: str, font_path: Path, quality: int) -> bytes:
    """Плашка в правом нижнем углу: белый текст на полупрозрачном чёрном."""
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    size = max(MIN_FONT_PX, base.height // FONT_HEIGHT_RATIO)
    font = _font(font_path, size)
    pad = size // 2
    margin = int(size * MARGIN_RATIO)

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    box_w, box_h = right - left + 2 * pad, bottom - top + 2 * pad
    x1, y1 = base.width - margin, base.height - margin
    x0, y0 = x1 - box_w, y1 - box_h
    draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0, PLATE_ALPHA))
    draw.text((x0 + pad - left, y0 + pad - top), text, font=font, fill=(255, 255, 255, 255))

    out = io.BytesIO()
    Image.alpha_composite(base, overlay).convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()
