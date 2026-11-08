from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("gender", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_contacts_gender_allowed"),
        "contacts",
        "gender IN ('female', 'male')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_contacts_gender_allowed"), "contacts", type_="check")
    op.drop_column("contacts", "gender")
