from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

BUSY_TIMEOUT_MS = 5000


def sqlite_url(db_file: Path) -> str:
    return f"sqlite+aiosqlite:///{db_file.resolve().as_posix()}"


def _pragmas_listener(foreign_keys: bool) -> Any:
    def _set_pragmas(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
        # WAL: subprocess сбора пишет, пока сервер читает для панели
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.close()

    return _set_pragmas


def create_engine(db_file: Path, *, foreign_keys: bool = True, **kwargs: Any) -> AsyncEngine:
    """foreign_keys=False только для миграций: batch-режим пересоздаёт таблицу через DROP,
    и с ON DELETE CASCADE это удалило бы дочерние строки."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = _create_async_engine(sqlite_url(db_file), **kwargs)
    event.listen(engine.sync_engine, "connect", _pragmas_listener(foreign_keys))
    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
