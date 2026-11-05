"""business profile: trade and default invoice note

Revision ID: c4e1a7b2d9f0
Revises: 880d26f3a433
Create Date: 2026-11-05 10:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e1a7b2d9f0"
down_revision: str | None = "880d26f3a433"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("trade", sa.String(length=40), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("invoice_note", sa.String(length=1000), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_column("invoice_note")
        batch_op.drop_column("trade")
