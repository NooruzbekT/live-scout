import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Dialect, ForeignKey, MetaData, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[dt.datetime]):
    """SQLite не хранит часовой пояс: пишем naive UTC, читаем aware UTC.
    Naive datetime на входе — ошибка, а не молчаливое «считаем, что UTC»."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: время должно быть с часовым поясом")
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"ожидался datetime, получено {type(value).__name__}")
        return value.replace(tzinfo=dt.UTC)


# именованные ограничения нужны batch-миграциям SQLite (пересоздание таблиц)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dt.datetime: UTCDateTime}


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("date", "slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date]
    slot: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # running | ok | partial | failed | aborted
    started_at: Mapped[dt.datetime]
    finished_at: Mapped[dt.datetime | None]
    rooms_opened: Mapped[int] = mapped_column(default=0)
    rooms_failed: Mapped[int] = mapped_column(default=0)
    selling_found: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)

    streams: Mapped[list["Stream"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class Stream(Base):
    __tablename__ = "streams"
    __table_args__ = (
        UniqueConstraint("date", "slot", "username"),
        UniqueConstraint("run_id", "n"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]
    slot: Mapped[str] = mapped_column(String(16))
    # порядок продающих в run (max+1); № в отчёте пересчитывается 1..N при сборке
    n: Mapped[int | None]
    username: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(256))
    room_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    viewers: Mapped[int]
    followers: Mapped[int]
    live_started_at: Mapped[dt.datetime | None]
    captured_at: Mapped[dt.datetime]
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(128))  # search:<запрос> | following | feed
    screenshot_path: Mapped[str] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text)
    chat_json: Mapped[str] = mapped_column(Text)

    is_selling: Mapped[bool | None]
    confidence: Mapped[float | None]
    category: Mapped[str | None] = mapped_column(String(64))
    products_json: Mapped[str | None] = mapped_column(Text)
    prices_json: Mapped[str | None] = mapped_column(Text)
    ai_reason: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(String(64))
    ai_raw: Mapped[str | None] = mapped_column(Text)

    manual_override: Mapped[bool] = mapped_column(default=False)
    excluded: Mapped[bool] = mapped_column(default=False)

    drive_file_id: Mapped[str | None] = mapped_column(String(128))
    drive_url: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="streams")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(unique=True)
    spreadsheet_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    built_at: Mapped[dt.datetime]
    rows_morning: Mapped[int] = mapped_column(default=0)
    rows_noon: Mapped[int] = mapped_column(default=0)
    rows_evening: Mapped[int] = mapped_column(default=0)
