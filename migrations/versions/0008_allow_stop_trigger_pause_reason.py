from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_contacts_pause_reason_allowed"), "contacts", type_="check")
    op.create_check_constraint(
        op.f("ck_contacts_pause_reason_allowed"),
        "contacts",
        "pause_reason IN ('manual_message', 'send_rejected', 'owner', 'stop_trigger')",
    )


def downgrade() -> None:
    op.execute("UPDATE contacts SET pause_reason = 'owner' WHERE pause_reason = 'stop_trigger'")
    op.drop_constraint(op.f("ck_contacts_pause_reason_allowed"), "contacts", type_="check")
    op.create_check_constraint(
        op.f("ck_contacts_pause_reason_allowed"),
        "contacts",
        "pause_reason IN ('manual_message', 'send_rejected', 'owner')",
    )
