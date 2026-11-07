from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="scheduled", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("text <> ''", name=op.f("ck_scheduled_messages_text_not_empty")),
        sa.CheckConstraint(
            "state IN ('scheduled', 'sending', 'sent', 'failed', 'cancelled')",
            name=op.f("ck_scheduled_messages_state_allowed"),
        ),
        sa.CheckConstraint(
            "source IN ('autopilot', 'manual_test')",
            name=op.f("ck_scheduled_messages_source_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_scheduled_messages_contact_id_contacts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_messages")),
    )
    op.create_index(
        "uq_scheduled_messages_contact_id_trigger_message_id",
        "scheduled_messages",
        ["contact_id", "trigger_message_id"],
        unique=True,
        postgresql_where=sa.text("trigger_message_id IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_scheduled_messages_state_send_at"),
        "scheduled_messages",
        ["state", "send_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_scheduled_messages_state_send_at"), table_name="scheduled_messages")
    op.drop_index(
        "uq_scheduled_messages_contact_id_trigger_message_id",
        table_name="scheduled_messages",
        postgresql_where=sa.text("trigger_message_id IS NOT NULL"),
    )
    op.drop_table("scheduled_messages")
