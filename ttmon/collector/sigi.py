import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

LIVE_STATUS = 2
AUDIO_ONLY_QUALITY = "ao"
# от худшего к лучшему: при отсутствии ao берём самый лёгкий поток
VIDEO_QUALITIES_ASC = ("ld", "sd", "hd", "uhd", "origin")


class SigiError(ValueError):
    pass


class RoomInfo(BaseModel):
    username: str
    nickname: str
    room_id: str
    title: str
    status: int
    viewers: int
    followers: int
    live_started_at: datetime | None
    audio_url: str | None

    @property
    def is_live(self) -> bool:
        return self.status == LIVE_STATUS


def _get(data: Any, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise SigiError(f"нет поля {'.'.join(path)}")
        cur = cur[key]
    return cur


def pick_audio_url(stream_data: str) -> str | None:
    """stream_data — строка с JSON внутри SIGI_STATE."""
    try:
        qualities = json.loads(stream_data)["data"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(qualities, dict):
        return None

    def flv(quality: str) -> str | None:
        url = qualities.get(quality, {}).get("main", {}).get("flv")
        return url if isinstance(url, str) and url else None

    for quality in (AUDIO_ONLY_QUALITY, *VIDEO_QUALITIES_ASC):
        if url := flv(quality):
            return url
    return None


def parse_sigi(data: Any) -> RoomInfo:
    info = _get(data, "LiveRoom", "liveRoomUserInfo")
    user = _get(info, "user")
    room = _get(info, "liveRoom")
    start = room.get("startTime") or 0
    stream_data = room.get("streamData", {}).get("pull_data", {}).get("stream_data", "")
    try:
        return RoomInfo(
            username=_get(user, "uniqueId"),
            nickname=user.get("nickname") or "",
            room_id=str(_get(user, "roomId")),
            title=room.get("title") or "",
            status=_get(room, "status"),
            viewers=room.get("liveRoomStats", {}).get("userCount") or 0,
            followers=info.get("stats", {}).get("followerCount") or 0,
            live_started_at=datetime.fromtimestamp(start, UTC) if start else None,
            audio_url=pick_audio_url(stream_data) if isinstance(stream_data, str) else None,
        )
    except ValidationError as exc:
        raise SigiError(str(exc)) from exc
