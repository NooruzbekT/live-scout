import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ttmon.ai.schema import Classification, Price
from ttmon.collector.run import audio_path, merge_voice_prices, needs_voice
from ttmon.collector.sigi import RoomInfo
from ttmon.media.audio import ffmpeg_exe, record_audio
from ttmon.settings import load_config

CFG = load_config()


@pytest.fixture
def tone(tmp_path: Path) -> Path:
    """Локальный файл вместо потока TikTok: ffmpeg тот же, сеть не нужна."""
    path = tmp_path / "tone.wav"
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", str(path)],
        check=True,
    )  # fmt: skip
    return path


async def test_record_audio_writes_mp3(tone: Path, tmp_path: Path) -> None:
    out = tmp_path / "audio" / "2026-10-23" / "test" / "shop_1200.mp3"

    result = await record_audio(str(tone), out, 3)

    assert result == out
    assert out.read_bytes()[:3] == b"ID3" or out.read_bytes()[:2] == b"\xff\xfb"


async def test_record_audio_without_ffmpeg(tmp_path: Path) -> None:
    out = tmp_path / "shop_1200.mp3"

    assert await record_audio("x.flv", out, 3, exe=str(tmp_path / "no-ffmpeg.exe")) is None
    assert not out.exists()


async def test_record_audio_failure_leaves_no_file(tmp_path: Path) -> None:
    out = tmp_path / "shop_1200.mp3"

    assert await record_audio(str(tmp_path / "missing.flv"), out, 3) is None
    assert not out.exists()


def _verdict(prices: list[int]) -> Classification:
    return Classification(
        is_selling=True,
        confidence=0.9,
        category="Другое",
        prices=[Price(raw=str(a), amount=a, source="screen") for a in prices],
        reason="тест",
    )


@pytest.mark.parametrize(
    ("mode", "selling", "prices", "expected"),
    [
        ("auto", True, [], True),
        ("auto", True, [3500], False),
        ("auto", False, [], False),
        ("auto", None, [], False),
        ("always", True, [3500], True),
        ("always", False, [], False),
        ("never", True, [], False),
    ],
)
def test_needs_voice(mode: str, selling: bool | None, prices: list[int], expected: bool) -> None:
    assert needs_voice(mode, selling, _verdict(prices)) is expected


def test_needs_voice_without_answer() -> None:
    assert needs_voice("always", True, None) is False


def test_voice_prices_merged_without_duplicates() -> None:
    c = _verdict([3500])
    voice = [
        Price(item="Куртка", raw="три пятьсот", amount=3500, source="voice"),
        Price(item="Платье", raw="бес мың", amount=5000, source="voice"),
        Price(item="платье", raw="пять тысяч", amount=5000, source="voice"),
        Price(item="Юбка", raw="пять тысяч", amount=5000, source="voice"),
    ]

    assert merge_voice_prices(c, voice) == 2
    assert [(p.item, p.amount, p.source) for p in c.prices] == [
        (None, 3500, "screen"),
        ("Платье", 5000, "voice"),
        ("Юбка", 5000, "voice"),
    ]


def _info(username: str) -> RoomInfo:
    return RoomInfo(
        username=username,
        nickname="",
        room_id="1",
        title="",
        status=2,
        viewers=1,
        followers=0,
        live_started_at=datetime(2026, 10, 23, tzinfo=UTC),
        audio_url="https://example.invalid/a.flv",
    )


def test_audio_path_under_date_and_slot(tmp_path: Path) -> None:
    path = audio_path(tmp_path, _info("shop.almaty"), "noon", CFG)

    assert path.parent.parent.parent == tmp_path
    assert path.parent.name == "noon"
    assert path.name.startswith("shop.almaty_") and path.suffix == ".mp3"


def test_audio_path_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="вне"):
        audio_path(tmp_path, _info("../../../evil"), "noon", CFG)
