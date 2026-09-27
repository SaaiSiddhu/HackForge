"""Add indexes for projects title, track_id, and team_id.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_projects_title", "projects", ["title"], unique=False)
    op.create_index("ix_projects_track_id", "projects", ["track_id"], unique=False)
    op.create_index("ix_projects_team_id", "projects", ["team_id"], unique=False)


def downgrade():
    op.drop_index("ix_projects_team_id", table_name="projects")
    op.drop_index("ix_projects_track_id", table_name="projects")
    op.drop_index("ix_projects_title", table_name="projects")
