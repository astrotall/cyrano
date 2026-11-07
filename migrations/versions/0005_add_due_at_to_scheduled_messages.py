from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_DUE_AT = "UPDATE scheduled_messages SET due_at = send_at"


def upgrade() -> None:
    op.add_column(
        "scheduled_messages",
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(BACKFILL_DUE_AT)
    op.alter_column("scheduled_messages", "due_at", nullable=False)


def downgrade() -> None:
    op.drop_column("scheduled_messages", "due_at")
