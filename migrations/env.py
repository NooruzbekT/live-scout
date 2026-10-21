import asyncio
from logging.config import fileConfig
from pathlib import Path
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from ttmon.db.models import Base, UTCDateTime
from ttmon.db.session import create_engine, sqlite_url
from ttmon.settings import get_config, project_path

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_file() -> Path:
    # attributes["db_file"] — программный вызов (тесты), -x db_file — CLI
    if "db_file" in config.attributes:
        return Path(config.attributes["db_file"])
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_file" in x_args:
        return Path(x_args["db_file"])
    return project_path(get_config().paths.db_file)


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # файлы ревизий не импортируют ttmon: UTCDateTime рендерится как sa.DateTime()
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=_render_item,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=sqlite_url(_db_file()), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_db_file(), foreign_keys=False, poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
