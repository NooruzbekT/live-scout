"""initial

Revision ID: 0001
Revises:
Create Date: 2026-10-15 20:03:46.655962

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("spreadsheet_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("built_at", sa.DateTime(), nullable=False),
        sa.Column("rows_morning", sa.Integer(), nullable=False),
        sa.Column("rows_noon", sa.Integer(), nullable=False),
        sa.Column("rows_evening", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
        sa.UniqueConstraint("date", name=op.f("uq_reports_date")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("rooms_opened", sa.Integer(), nullable=False),
        sa.Column("rooms_failed", sa.Integer(), nullable=False),
        sa.Column("selling_found", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("date", "slot", name=op.f("uq_runs_date_slot")),
    )
    op.create_table(
        "streams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("n", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("nickname", sa.String(length=256), nullable=False),
        sa.Column("room_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("viewers", sa.Integer(), nullable=False),
        sa.Column("followers", sa.Integer(), nullable=False),
        sa.Column("live_started_at", sa.DateTime(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("screenshot_path", sa.Text(), nullable=False),
        sa.Column("audio_path", sa.Text(), nullable=True),
        sa.Column("chat_json", sa.Text(), nullable=False),
        sa.Column("is_selling", sa.Boolean(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("products_json", sa.Text(), nullable=True),
        sa.Column("prices_json", sa.Text(), nullable=True),
        sa.Column("ai_reason", sa.Text(), nullable=True),
        sa.Column("ai_model", sa.String(length=64), nullable=True),
        sa.Column("ai_raw", sa.Text(), nullable=True),
        sa.Column("manual_override", sa.Boolean(), nullable=False),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("drive_file_id", sa.String(length=128), nullable=True),
        sa.Column("drive_url", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_streams_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_streams")),
        sa.UniqueConstraint("date", "slot", "username", name=op.f("uq_streams_date_slot_username")),
        sa.UniqueConstraint("run_id", "n", name=op.f("uq_streams_run_id_n")),
    )
    with op.batch_alter_table("streams", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_streams_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("streams", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_streams_run_id"))

    op.drop_table("streams")
    op.drop_table("runs")
    op.drop_table("reports")
