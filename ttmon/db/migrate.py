from pathlib import Path

from alembic import command
from alembic.config import Config

from ttmon.settings import PROJECT_ROOT


def upgrade_to_head(db_file: Path) -> None:
    """Синхронно: migrations/env.py сам вызывает asyncio.run, из работающего loop нельзя."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.attributes["db_file"] = str(db_file)
    cfg.attributes["skip_logging_config"] = True
    command.upgrade(cfg, "head")
