from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_DEFAULT_MODE = """
INSERT INTO modes (name, prompt_fragment, few_shot_examples)
VALUES ('default', 'Keep a neutral, friendly tone.', '[]'::jsonb)
ON CONFLICT (name) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "modes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt_fragment", sa.Text(), nullable=False),
        sa.Column(
            "few_shot_examples",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modes")),
        sa.UniqueConstraint("name", name=op.f("uq_modes_name")),
    )
    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("new", "pending", "active", "paused", "ignored", name="contact_status"),
            server_default="new",
            nullable=False,
        ),
        sa.Column("mode_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["mode_id"], ["modes.id"], name=op.f("fk_contacts_mode_id_modes")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_contacts_tg_id")),
    )
    op.create_index(op.f("ix_contacts_status"), "contacts", ["status"], unique=False)
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "direction", sa.Enum("incoming", "outgoing", name="message_direction"), nullable=False
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"], ["contacts.id"], name=op.f("fk_messages_contact_id_contacts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "contact_id", "tg_message_id", name=op.f("uq_messages_contact_id_tg_message_id")
        ),
    )
    op.create_index(
        op.f("ix_messages_contact_id_created_at"),
        "messages",
        ["contact_id", "created_at"],
        unique=False,
    )
    op.execute(SEED_DEFAULT_MODE)


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_contact_id_created_at"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_contacts_status"), table_name="contacts")
    op.drop_table("contacts")
    op.drop_table("modes")
    op.execute("DROP TYPE message_direction")
    op.execute("DROP TYPE contact_status")
