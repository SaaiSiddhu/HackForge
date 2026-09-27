"""Phase 2 core workflows schema: accounts, sessions, roles, teams, invitations, questions, audit logs, project draft lifecycle.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Accounts
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_accounts_email", "accounts", ["email"], unique=True)

    # 2. Sessions
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_sessions_account_id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_account_id", "sessions", ["account_id"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)

    # 3. Event Roles
    op.create_table(
        "event_roles",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("event_id", sa.String(length=100), sa.ForeignKey("events.id", name="fk_event_roles_event_id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_event_roles_account_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "account_id", "role", name="uq_event_account_role"),
    )
    op.create_index("ix_event_roles_event_id", "event_roles", ["event_id"], unique=False)
    op.create_index("ix_event_roles_account_id", "event_roles", ["account_id"], unique=False)

    # 4. Add columns to events using batch mode for SQLite compatibility
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("registration_open", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("registration_close", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("submissions_open", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("min_team_size", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("max_team_size", sa.Integer(), nullable=False, server_default="5"))
        batch_op.add_column(sa.Column("prizes", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("created_by_account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_events_created_by_account_id", ondelete="SET NULL"), nullable=True))

    # 5. Event Questions
    op.create_table(
        "event_questions",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("event_id", sa.String(length=100), sa.ForeignKey("events.id", name="fk_event_questions_event_id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("question_type", sa.String(length=50), nullable=False),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("order_idx", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_event_questions_event_id", "event_questions", ["event_id"], unique=False)

    # 6. Event Audit Logs
    op.create_table(
        "event_audit_logs",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("event_id", sa.String(length=100), sa.ForeignKey("events.id", name="fk_event_audit_logs_event_id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_event_audit_logs_account_id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_audit_logs_event_id", "event_audit_logs", ["event_id"], unique=False)

    # 7. Add columns to teams
    with op.batch_alter_table("teams") as batch_op:
        batch_op.add_column(sa.Column("created_by_account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_teams_created_by_account_id", ondelete="SET NULL"), nullable=True))
        batch_op.add_column(sa.Column("captain_account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_teams_captain_account_id", ondelete="SET NULL"), nullable=True))

    # 8. Team Memberships with single team per account per event constraint
    op.create_table(
        "team_memberships",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("team_id", sa.String(length=100), sa.ForeignKey("teams.id", name="fk_team_memberships_team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(length=100), sa.ForeignKey("accounts.id", name="fk_team_memberships_account_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", sa.String(length=100), sa.ForeignKey("events.id", name="fk_team_memberships_event_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="member"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "account_id", name="uq_team_memberships_event_account"),
    )
    op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"], unique=False)
    op.create_index("ix_team_memberships_account_id", "team_memberships", ["account_id"], unique=False)
    op.create_index("ix_team_memberships_event_id", "team_memberships", ["event_id"], unique=False)

    # 9. Team Invitations
    op.create_table(
        "team_invitations",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("team_id", sa.String(length=100), sa.ForeignKey("teams.id", name="fk_team_invitations_team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_team_invitations_token", "team_invitations", ["token"], unique=True)
    op.create_index("ix_team_invitations_team_id", "team_invitations", ["team_id"], unique=False)
    op.create_index("ix_team_invitations_email", "team_invitations", ["email"], unique=False)

    # 10. Add columns to projects
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("tagline", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("demo_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("video_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("tech_tags", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("status", sa.String(length=50), nullable=False, server_default="submitted"))
        batch_op.add_column(sa.Column("thumbnail_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("gallery_urls", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("answers", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_projects_status", ["status"], unique=False)


def downgrade():
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_status")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("version")
        batch_op.drop_column("answers")
        batch_op.drop_column("gallery_urls")
        batch_op.drop_column("thumbnail_url")
        batch_op.drop_column("status")
        batch_op.drop_column("tech_tags")
        batch_op.drop_column("video_url")
        batch_op.drop_column("demo_url")
        batch_op.drop_column("description")
        batch_op.drop_column("tagline")

    op.drop_table("team_invitations")
    op.drop_table("team_memberships")

    with op.batch_alter_table("teams") as batch_op:
        batch_op.drop_column("captain_account_id")
        batch_op.drop_column("created_by_account_id")

    op.drop_table("event_audit_logs")
    op.drop_table("event_questions")

    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("created_by_account_id")
        batch_op.drop_column("prizes")
        batch_op.drop_column("max_team_size")
        batch_op.drop_column("min_team_size")
        batch_op.drop_column("submissions_open")
        batch_op.drop_column("registration_close")
        batch_op.drop_column("registration_open")
        batch_op.drop_column("description")

    op.drop_table("event_roles")
    op.drop_table("sessions")
    op.drop_table("accounts")
