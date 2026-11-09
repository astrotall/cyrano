from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_messages",
        sa.Column("reply_to_tg_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduled_messages", "reply_to_tg_message_id")
