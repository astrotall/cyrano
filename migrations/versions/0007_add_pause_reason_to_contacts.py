"""Add pause_reason to contacts, the reason a contact is paused."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("pause_reason", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        op.f("ck_contacts_pause_reason_allowed"),
        "contacts",
        "pause_reason IN ('manual_message', 'send_rejected', 'owner')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_contacts_pause_reason_allowed"), "contacts", type_="check")
    op.drop_column("contacts", "pause_reason")
