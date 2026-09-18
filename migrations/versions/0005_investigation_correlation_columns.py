"""add investigations.playbook_id, investigations.environment

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-17

Cross-server pattern correlation (find_similar in
gateway.domain.investigation_store) needs to filter by playbook/environment
alongside excluding the asking server — same reasoning as 0003's server_id
column: an indexed column, not a scan/filter over the `target` JSON blob.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("investigations")}
    if "playbook_id" not in columns:
        op.add_column("investigations", sa.Column("playbook_id", sa.String(), nullable=True))
        op.create_index("ix_investigations_playbook_id", "investigations", ["playbook_id"])
    if "environment" not in columns:
        op.add_column("investigations", sa.Column("environment", sa.String(), nullable=True))
        op.create_index("ix_investigations_environment", "investigations", ["environment"])


def downgrade() -> None:
    op.drop_index("ix_investigations_environment", table_name="investigations")
    op.drop_column("investigations", "environment")
    op.drop_index("ix_investigations_playbook_id", table_name="investigations")
    op.drop_column("investigations", "playbook_id")
