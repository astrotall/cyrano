from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_AUTOPILOT_STATE = "INSERT INTO autopilot_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"


def upgrade() -> None:
    op.add_column("contacts", sa.Column("reply_after", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "autopilot_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("stopped", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_autopilot_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_autopilot_state")),
    )
    op.execute(SEED_AUTOPILOT_STATE)


def downgrade() -> None:
    op.drop_table("autopilot_state")
    op.drop_column("contacts", "reply_after")
