import asyncio
import contextlib
import functools
from pathlib import Path

import imageio_ffmpeg
import structlog

log = structlog.get_logger(__name__)

# запас сверх длительности записи: подключение к CDN и буфер потока
CONNECT_TIMEOUT_S = 20
# 20 с mono 48 кбит/с ≈ 120 КБ; меньше — поток оборвался в самом начале
MIN_AUDIO_BYTES = 8_000


@functools.cache
def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg_args(url: str, out: Path, seconds: int) -> list[str]:
    return [
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-t", str(seconds), "-i", url,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out),
    ]  # fmt: skip


def _discard(out: Path) -> None:
    out.unlink(missing_ok=True)


def _recorded_size(out: Path) -> int:
    return out.stat().st_size if out.exists() else 0


async def record_audio(url: str, out: Path, seconds: int, exe: str | None = None) -> Path | None:
    """None — звук не записан (нет ffmpeg, ошибка потока, таймаут, короткий файл): эфир от
    этого не зависит. Ссылка потока подписана токеном, поэтому ни она, ни stderr ffmpeg
    (в нём ссылка) в лог не идут. Отмена задачи убивает ffmpeg и удаляет недописанный файл."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            exe or ffmpeg_exe(),
            *ffmpeg_args(url, out, seconds),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, RuntimeError) as exc:
        # RuntimeError — imageio_ffmpeg не нашёл бинарник
        log.warning("audio_failed", file=out.name, error=type(exc).__name__)
        return None
    try:
        code = await asyncio.wait_for(proc.wait(), seconds + CONNECT_TIMEOUT_S)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        _discard(out)
        if isinstance(exc, TimeoutError):
            log.warning("audio_failed", file=out.name, error="timeout")
            return None
        raise
    size = _recorded_size(out)
    if code != 0 or size < MIN_AUDIO_BYTES:
        _discard(out)
        log.warning("audio_failed", file=out.name, exit_code=code, size=size)
        return None
    return out
