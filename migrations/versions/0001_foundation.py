"""Initial fixture-backed portal schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("events", sa.Column("id", sa.String(100), primary_key=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("submissions_close", sa.DateTime(timezone=True), nullable=False))
    for name, extra in [
        ("tracks", []),
        ("teams", [sa.Column("members", sa.JSON(), nullable=False)]),
        ("judges", [sa.Column("email", sa.String(320), nullable=False), sa.Column("tracks", sa.JSON(), nullable=False)]),
    ]:
        op.create_table(name, sa.Column("id", sa.String(100), primary_key=True), sa.Column("event_id", sa.String(100), sa.ForeignKey("events.id"), nullable=False), sa.Column("name", sa.String(200), nullable=False), *extra)
    op.create_table("projects",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("event_id", sa.String(100), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("team_id", sa.String(100), sa.ForeignKey("teams.id"), nullable=False),
        sa.Column("track_id", sa.String(100), sa.ForeignKey("tracks.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("reviews",
        sa.Column("judge_id", sa.String(100), sa.ForeignKey("judges.id"), primary_key=True),
        sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False))


def downgrade():
    for name in ["reviews", "projects", "judges", "teams", "tracks", "events"]:
        op.drop_table(name)
