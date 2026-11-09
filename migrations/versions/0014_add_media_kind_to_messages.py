from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_kind", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_messages_media_kind_allowed"),
        "messages",
        "media_kind IN ('voice', 'video_note', 'photo', 'video', 'sticker', 'other')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_messages_media_kind_allowed"), "messages", type_="check")
    op.drop_column("messages", "media_kind")
