import io

from PIL import Image


def fit_width(image_jpeg: bytes, max_width: int, quality: int) -> bytes:
    """Копия для AI: уже узкие кадры не трогаем, чтобы не пережимать JPEG."""
    image = Image.open(io.BytesIO(image_jpeg))
    if image.width <= max_width:
        return image_jpeg
    height = round(image.height * max_width / image.width)
    out = io.BytesIO()
    image.convert("RGB").resize((max_width, height), Image.Resampling.LANCZOS).save(
        out, "JPEG", quality=quality
    )
    return out.getvalue()
