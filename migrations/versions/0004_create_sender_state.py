from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_SENDER_STATE = "INSERT INTO sender_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"


def upgrade() -> None:
    op.create_table(
        "sender_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("stopped", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_sender_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_state")),
    )
    op.execute(SEED_SENDER_STATE)
    op.create_index(
        op.f("ix_messages_direction_created_at"),
        "messages",
        ["direction", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_direction_created_at"), table_name="messages")
    op.drop_table("sender_state")
