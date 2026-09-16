"""add server_catalogs; drop orphaned pre-catalog tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16

`server_catalogs` (`ServerCatalogRecord`) was added to the ORM models after
0001 had already been applied to real databases. 0001 builds its schema via
a one-time `Base.metadata.create_all()` rather than explicit
`op.create_table()` calls (see that revision's own docstring), so any
database migrated before `server_catalogs` existed in the models module
never got the table — `alembic upgrade head` is a no-op there, since 0001
is already stamped applied and never re-runs. This surfaced as a hard
`UndefinedTableError` on every catalog-dependent tool call against such a
database. This revision creates the table explicitly so it lands
everywhere, not just on databases migrated fresh from an up-to-date
models.py.

Also drops `database_environments`, `database_inventory`, and
`database_policies` — tables from an earlier schema iteration with no
matching ORM model today (environment/policy configuration now lives in
config/*.yaml, not the database). Nothing in the current codebase reads or
writes them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # On a database migrated fresh from today's models.py, 0001's
    # `create_all()` already created this table (it builds from whatever
    # `Base.metadata` is *at the moment migrations run*, not a fixed
    # snapshot) — only a database that was already at 0001 before
    # `server_catalogs` existed in models.py is actually missing it.
    if "server_catalogs" not in existing:
        op.create_table(
            "server_catalogs",
            sa.Column("server_id", sa.String(), primary_key=True),
            sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("engine_version", sa.String(), nullable=False),
            sa.Column("engine_edition", sa.String(), nullable=False),
            sa.Column("catalog", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    for orphaned in ("database_environments", "database_inventory", "database_policies"):
        if orphaned in existing:
            op.drop_table(orphaned)


def downgrade() -> None:
    op.drop_table("server_catalogs")
    # The dropped tables' original schemas predate any model this codebase
    # still defines, so they aren't reconstructable here — recovering them
    # means restoring from a backup taken before this revision.
