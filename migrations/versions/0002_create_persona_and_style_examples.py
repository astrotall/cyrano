"""Create the persona and style_examples tables."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "persona",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_persona_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_persona")),
    )
    op.create_table(
        "style_examples",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("turns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_style_examples")),
        sa.UniqueConstraint("content_hash", name=op.f("uq_style_examples_content_hash")),
    )


def downgrade() -> None:
    op.drop_table("style_examples")
    op.drop_table("persona")
