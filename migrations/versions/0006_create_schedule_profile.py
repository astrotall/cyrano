"""Create the schedule_profile table, one row at most; no row means the default profile."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_profile",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_schedule_profile_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_profile")),
    )


def downgrade() -> None:
    op.drop_table("schedule_profile")
