"""add llm_decision_events

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17

A genuinely new table (not one that existed since 0001 and needed a
retrofitted column, like 0003) — durable storage for a handful of decision-
quality signals (a conclusion rejected by grounding/verification/self-
critique, a cross-provider fallback substitution) that previously only
existed as structured log lines. See gateway.domain.decision_events for
which events and why not all of them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "llm_decision_events" not in set(inspector.get_table_names()):
        op.create_table(
            "llm_decision_events",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("conversation_id", sa.String(), nullable=True),
            sa.Column("investigation_id", sa.String(), nullable=True),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("model", sa.String(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_llm_decision_events_conversation_id", "llm_decision_events", ["conversation_id"]
        )
        op.create_index(
            "ix_llm_decision_events_investigation_id", "llm_decision_events", ["investigation_id"]
        )
        op.create_index("ix_llm_decision_events_event_type", "llm_decision_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("llm_decision_events")
