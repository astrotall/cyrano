from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_scheduled_messages_source_allowed"), "scheduled_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_scheduled_messages_source_allowed"),
        "scheduled_messages",
        "source IN ('autopilot', 'manual_test', 'proactive')",
    )
    op.add_column(
        "contacts",
        sa.Column("proactive_not_before", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "proactive_not_before")
    op.execute(
        "UPDATE scheduled_messages SET state = 'cancelled', cancel_reason = 'downgrade' "
        "WHERE source = 'proactive' AND state = 'scheduled'"
    )
    op.execute("UPDATE scheduled_messages SET source = 'autopilot' WHERE source = 'proactive'")
    op.drop_constraint(
        op.f("ck_scheduled_messages_source_allowed"), "scheduled_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_scheduled_messages_source_allowed"),
        "scheduled_messages",
        "source IN ('autopilot', 'manual_test')",
    )
