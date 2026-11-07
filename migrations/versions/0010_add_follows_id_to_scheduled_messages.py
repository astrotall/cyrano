from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANCEL_WAITING_FOLLOWERS = (
    "UPDATE scheduled_messages SET state = 'cancelled', cancel_reason = 'predecessor' "
    "WHERE follows_id IS NOT NULL AND state = 'scheduled'"
)


def upgrade() -> None:
    op.add_column("scheduled_messages", sa.Column("follows_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_scheduled_messages_follows_id_scheduled_messages"),
        "scheduled_messages",
        "scheduled_messages",
        ["follows_id"],
        ["id"],
    )
    op.create_index(
        op.f("ix_scheduled_messages_follows_id"),
        "scheduled_messages",
        ["follows_id"],
        unique=False,
    )


def downgrade() -> None:
    op.execute(CANCEL_WAITING_FOLLOWERS)
    op.drop_index(op.f("ix_scheduled_messages_follows_id"), table_name="scheduled_messages")
    op.drop_constraint(
        op.f("fk_scheduled_messages_follows_id_scheduled_messages"),
        "scheduled_messages",
        type_="foreignkey",
    )
    op.drop_column("scheduled_messages", "follows_id")
