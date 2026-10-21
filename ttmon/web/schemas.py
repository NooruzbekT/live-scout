import datetime as dt

from pydantic import BaseModel, ConfigDict


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: dt.date
    slot: str
    status: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    rooms_opened: int
    rooms_failed: int
    selling_found: int
    error: str | None


class StreamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: dt.date
    slot: str
    n: int | None
    username: str
    nickname: str
    url: str
    captured_at: dt.datetime
    is_selling: bool | None
    confidence: float | None
    category: str | None
    prices_text: str
    ai_reason: str | None
    manual_override: bool
    excluded: bool
    screenshot_url: str | None
    drive_url: str | None


class StreamPatch(BaseModel):
    """prices_text — как в отчёте: «Пижама 5 шт: 12 500; Худи: 3000», пусто — цен нет."""

    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    prices_text: str | None = None
    is_selling: bool | None = None
    excluded: bool | None = None


class JobOut(BaseModel):
    name: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    exit_code: int | None
    tail: list[str]


class JobStarted(BaseModel):
    job: str


class HealthOut(BaseModel):
    ok: bool
    logged_in: bool | None
    running_job: str | None
    next_runs: dict[str, dt.datetime | None]
