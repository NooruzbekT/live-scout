import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ttmon.collector.sigi import SigiError, parse_sigi, pick_audio_url

FIXTURE = Path(__file__).parent / "fixtures" / "sigi_state_live.json"


@pytest.fixture
def sigi() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_parse_live_room(sigi: dict[str, Any]) -> None:
    info = parse_sigi(sigi)
    assert info.username == "shop.almaty.test"
    assert info.nickname == "Женская одежда Алматы"
    assert info.room_id == "7000000000000000001"
    assert info.is_live
    assert info.viewers == 144
    assert info.followers == 12477
    assert info.live_started_at == datetime.fromtimestamp(1792079014, UTC)
    assert info.audio_url is not None and "only_audio=1" in info.audio_url


def test_ended_room(sigi: dict[str, Any]) -> None:
    sigi["LiveRoom"]["liveRoomUserInfo"]["liveRoom"]["status"] = 4
    assert not parse_sigi(sigi).is_live


def test_zero_start_time_is_none(sigi: dict[str, Any]) -> None:
    sigi["LiveRoom"]["liveRoomUserInfo"]["liveRoom"]["startTime"] = 0
    assert parse_sigi(sigi).live_started_at is None


@pytest.mark.parametrize(
    "path",
    [
        ("LiveRoom",),
        ("LiveRoom", "liveRoomUserInfo", "user", "uniqueId"),
        ("LiveRoom", "liveRoomUserInfo", "liveRoom", "status"),
    ],
)
def test_missing_required_field(sigi: dict[str, Any], path: tuple[str, ...]) -> None:
    broken = copy.deepcopy(sigi)
    node = broken
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    with pytest.raises(SigiError):
        parse_sigi(broken)


def _stream_data(qualities: dict[str, str]) -> str:
    return json.dumps({"data": {q: {"main": {"flv": url}} for q, url in qualities.items()}})


@pytest.mark.parametrize(
    ("qualities", "expected"),
    [
        ({"hd": "hd.flv", "ao": "ao.flv"}, "ao.flv"),
        ({"hd": "hd.flv", "sd": "sd.flv"}, "sd.flv"),
        ({"hd": "hd.flv", "ao": ""}, "hd.flv"),
        ({}, None),
    ],
)
def test_pick_audio_url(qualities: dict[str, str], expected: str | None) -> None:
    assert pick_audio_url(_stream_data(qualities)) == expected


@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"data": []}'])
def test_pick_audio_url_garbage(raw: str) -> None:
    assert pick_audio_url(raw) is None
