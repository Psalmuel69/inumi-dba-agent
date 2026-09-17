"""add investigations.server_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17

`InvestigationRecord.target` is a JSON blob with no indexable server key.
Investigation memory recall (looking up recent findings for a server before
starting a new investigation) and cross-server pattern correlation both need
to query "which investigations were about this server" directly, which a
JSON scan can't do efficiently or portably across SQLite/Postgres. This adds
an explicit, indexed `server_id` column alongside `target` rather than
parsing it out of the JSON at query time.

Existing rows get `server_id = NULL` — there is no reliable way to derive it
from `target` after the fact (its shape has never been contractually fixed),
and a NULL simply means "predates memory recall," not an error.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("investigations")}
    if "server_id" not in columns:
        op.add_column("investigations", sa.Column("server_id", sa.String(), nullable=True))
        op.create_index("ix_investigations_server_id", "investigations", ["server_id"])


def downgrade() -> None:
    op.drop_index("ix_investigations_server_id", table_name="investigations")
    op.drop_column("investigations", "server_id")
