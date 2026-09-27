"""Phase 4: event target_reviews_per_project and results snapshots table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add target_reviews_per_project to events
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column("target_reviews_per_project", sa.Integer(), nullable=False, server_default="3")
        )

    # 2. Table: event_results_snapshots
    op.create_table(
        "event_results_snapshots",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=100),
            sa.ForeignKey("events.id", name="fk_event_results_snapshots_event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by_account_id",
            sa.String(length=100),
            sa.ForeignKey("accounts.id", name="fk_event_results_snapshots_account_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("method", sa.String(length=100), nullable=False, server_default="zscore_v1"),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("input_review_hashes", sa.JSON(), nullable=False),
        sa.Column("results_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_results_snapshots_event_id", "event_results_snapshots", ["event_id"], unique=False)
    op.create_index("ix_event_results_snapshots_created_at", "event_results_snapshots", ["created_at"], unique=False)


def downgrade():
    op.drop_index("ix_event_results_snapshots_created_at", table_name="event_results_snapshots")
    op.drop_index("ix_event_results_snapshots_event_id", table_name="event_results_snapshots")
    op.drop_table("event_results_snapshots")

    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("target_reviews_per_project")
