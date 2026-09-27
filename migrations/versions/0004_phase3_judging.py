"""Phase 3 judging schema: judge invitations, permissions, assignments, rubrics, criterion scores.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""
from datetime import datetime, timezone
import json
import uuid
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    now = datetime.now(timezone.utc)

    # 1. Add account_id to judges
    with op.batch_alter_table("judges") as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_id",
                sa.String(length=100),
                sa.ForeignKey("accounts.id", name="fk_judges_account_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_judges_account_id", ["account_id"], unique=False)

    # 2. Table: judge_tracks
    op.create_table(
        "judge_tracks",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "judge_id",
            sa.String(length=100),
            sa.ForeignKey("judges.id", name="fk_judge_tracks_judge_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "track_id",
            sa.String(length=100),
            sa.ForeignKey("tracks.id", name="fk_judge_tracks_track_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("judge_id", "track_id", name="uq_judge_tracks_judge_track"),
    )
    op.create_index("ix_judge_tracks_judge_id", "judge_tracks", ["judge_id"], unique=False)
    op.create_index("ix_judge_tracks_track_id", "judge_tracks", ["track_id"], unique=False)

    # 3. Table: judge_invitations
    op.create_table(
        "judge_invitations",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=100),
            sa.ForeignKey("events.id", name="fk_judge_invitations_event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("tracks", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column(
            "invited_by_account_id",
            sa.String(length=100),
            sa.ForeignKey("accounts.id", name="fk_judge_invitations_invited_by", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "accepted_by_account_id",
            sa.String(length=100),
            sa.ForeignKey("accounts.id", name="fk_judge_invitations_accepted_by", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_judge_invitations_token_hash", "judge_invitations", ["token_hash"], unique=True)
    op.create_index("ix_judge_invitations_event_id", "judge_invitations", ["event_id"], unique=False)
    op.create_index("ix_judge_invitations_email", "judge_invitations", ["email"], unique=False)

    # 4. Table: judge_assignments
    op.create_table(
        "judge_assignments",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=100),
            sa.ForeignKey("events.id", name="fk_judge_assignments_event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "judge_id",
            sa.String(length=100),
            sa.ForeignKey("judges.id", name="fk_judge_assignments_judge_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=100),
            sa.ForeignKey("projects.id", name="fk_judge_assignments_project_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("judge_id", "project_id", name="uq_judge_assignments_judge_project"),
    )
    op.create_index("ix_judge_assignments_event_id", "judge_assignments", ["event_id"], unique=False)
    op.create_index("ix_judge_assignments_judge_id", "judge_assignments", ["judge_id"], unique=False)
    op.create_index("ix_judge_assignments_project_id", "judge_assignments", ["project_id"], unique=False)

    # 5. Table: rubrics
    op.create_table(
        "rubrics",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=100),
            sa.ForeignKey("events.id", name="fk_rubrics_event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(length=200), nullable=False, server_default="Default Rubric"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "version", name="uq_rubrics_event_version"),
    )
    op.create_index("ix_rubrics_event_id", "rubrics", ["event_id"], unique=False)

    # 6. Table: rubric_criteria
    op.create_table(
        "rubric_criteria",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column(
            "rubric_id",
            sa.String(length=100),
            sa.ForeignKey("rubrics.id", name="fk_rubric_criteria_rubric_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("min_score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("max_score", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("order_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("rubric_id", "key", name="uq_rubric_criteria_rubric_key"),
    )
    op.create_index("ix_rubric_criteria_rubric_id", "rubric_criteria", ["rubric_id"], unique=False)

    # 7. Add columns to reviews
    with op.batch_alter_table("reviews") as batch_op:
        batch_op.add_column(
            sa.Column(
                "event_id",
                sa.String(length=100),
                sa.ForeignKey("events.id", name="fk_reviews_event_id", ondelete="CASCADE"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "rubric_id",
                sa.String(length=100),
                sa.ForeignKey("rubrics.id", name="fk_reviews_rubric_id", ondelete="SET NULL"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("status", sa.String(length=50), nullable=False, server_default="finalized"))
        batch_op.add_column(sa.Column("raw_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_reviews_event_id", ["event_id"], unique=False)
        batch_op.create_index("ix_reviews_status", ["status"], unique=False)

    # 8. Table: review_criterion_scores
    op.create_table(
        "review_criterion_scores",
        sa.Column("id", sa.String(length=100), primary_key=True),
        sa.Column("judge_id", sa.String(length=100), nullable=False),
        sa.Column("project_id", sa.String(length=100), nullable=False),
        sa.Column(
            "criterion_id",
            sa.String(length=100),
            sa.ForeignKey("rubric_criteria.id", name="fk_rcs_criterion_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("criterion_key", sa.String(length=100), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["judge_id", "project_id"],
            ["reviews.judge_id", "reviews.project_id"],
            name="fk_rcs_review",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("judge_id", "project_id", "criterion_key", name="uq_review_criterion_scores_key"),
    )
    op.create_index("ix_review_criterion_scores_judge_proj", "review_criterion_scores", ["judge_id", "project_id"], unique=False)

    # 9. Data Migration & Backfill
    bind = op.get_bind()

    # Backfill default rubrics for each event
    events = bind.execute(sa.text("SELECT id FROM events")).fetchall()
    rubric_criteria_ids = {}

    for (ev_id,) in events:
        rubric_id = f"rb_{ev_id}_v1"
        bind.execute(
            sa.text(
                "INSERT INTO rubrics (id, event_id, version, name, is_active, created_at) "
                "VALUES (:id, :event_id, 1, 'Default Rubric', true, :created_at) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": rubric_id, "event_id": ev_id, "created_at": now},
        )

        for idx, (ckey, cname) in enumerate([
            ("functionality", "Functionality & Execution"),
            ("quality", "Code & Technical Quality"),
            ("innovation", "Innovation & Originality"),
        ]):
            cid = f"rc_{rubric_id}_{ckey}"
            rubric_criteria_ids[(ev_id, ckey)] = cid
            bind.execute(
                sa.text(
                    "INSERT INTO rubric_criteria (id, rubric_id, key, name, description, weight, min_score, max_score, order_idx) "
                    "VALUES (:id, :rubric_id, :key, :name, '', 1.0, 1.0, 5.0, :order_idx) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": cid, "rubric_id": rubric_id, "key": ckey, "name": cname, "order_idx": idx},
            )

    # Backfill judge_tracks from judges.tracks JSON column
    judges = bind.execute(sa.text("SELECT id, tracks FROM judges")).fetchall()
    for j_id, j_tracks in judges:
        if j_tracks:
            tracks_list = json.loads(j_tracks) if isinstance(j_tracks, str) else j_tracks
            if isinstance(tracks_list, list):
                for trk in tracks_list:
                    jt_id = f"jt_{j_id}_{trk}"
                    bind.execute(
                        sa.text(
                            "INSERT INTO judge_tracks (id, judge_id, track_id) "
                            "VALUES (:id, :judge_id, :track_id) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        {"id": jt_id, "judge_id": j_id, "track_id": trk},
                    )

    # Link demo judges to demo accounts if they exist
    bind.execute(
        sa.text(
            "UPDATE judges SET account_id = 'acc_jdg_a_demo' "
            "WHERE id = 'jdg_01' AND EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_jdg_a_demo')"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE judges SET account_id = 'acc_jdg_b_demo' "
            "WHERE id = 'jdg_02' AND EXISTS (SELECT 1 FROM accounts WHERE id = 'acc_jdg_b_demo')"
        )
    )

    # Backfill reviews: event_id, rubric_id, status, timestamps, raw_score, assignments, and criterion_scores
    reviews = bind.execute(
        sa.text(
            "SELECT r.judge_id, r.project_id, r.criteria, p.event_id "
            "FROM reviews r JOIN projects p ON r.project_id = p.id"
        )
    ).fetchall()

    for j_id, p_id, crit_raw, ev_id in reviews:
        crit_dict = json.loads(crit_raw) if isinstance(crit_raw, str) else (crit_raw or {})
        rubric_id = f"rb_{ev_id}_v1"

        # Calculate raw score: 100 * sum(weight * (score - min)/(max - min)) / sum(weights)
        # default min=1.0, max=5.0, weight=1.0
        tot_weighted_contrib = 0.0
        tot_weights = 0.0
        for ckey, cscore in crit_dict.items():
            if isinstance(cscore, (int, float)):
                cscore_f = float(cscore)
                contrib = (cscore_f - 1.0) / 4.0
                tot_weighted_contrib += 1.0 * contrib
                tot_weights += 1.0

                # Insert into review_criterion_scores
                rcs_id = f"rcs_{j_id}_{p_id}_{ckey}"
                crit_obj_id = rubric_criteria_ids.get((ev_id, ckey))
                bind.execute(
                    sa.text(
                        "INSERT INTO review_criterion_scores (id, judge_id, project_id, criterion_id, criterion_key, score) "
                        "VALUES (:id, :judge_id, :project_id, :criterion_id, :criterion_key, :score) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "id": rcs_id,
                        "judge_id": j_id,
                        "project_id": p_id,
                        "criterion_id": crit_obj_id,
                        "criterion_key": ckey,
                        "score": cscore_f,
                    },
                )

        raw_score = round(100.0 * (tot_weighted_contrib / tot_weights), 2) if tot_weights > 0 else None

        # Update review record
        bind.execute(
            sa.text(
                "UPDATE reviews SET event_id = :event_id, rubric_id = :rubric_id, status = 'finalized', "
                "raw_score = :raw_score, version = 1, created_at = :now, updated_at = :now "
                "WHERE judge_id = :judge_id AND project_id = :project_id"
            ),
            {
                "event_id": ev_id,
                "rubric_id": rubric_id,
                "raw_score": raw_score,
                "now": now,
                "judge_id": j_id,
                "project_id": p_id,
            },
        )

        # Backfill judge_assignments for this review
        asgn_id = f"asgn_{j_id}_{p_id}"
        bind.execute(
            sa.text(
                "INSERT INTO judge_assignments (id, event_id, judge_id, project_id, assigned_at) "
                "VALUES (:id, :event_id, :judge_id, :project_id, :assigned_at) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": asgn_id,
                "event_id": ev_id,
                "judge_id": j_id,
                "project_id": p_id,
                "assigned_at": now,
            },
        )


def downgrade():
    op.drop_table("review_criterion_scores")

    with op.batch_alter_table("reviews") as batch_op:
        batch_op.drop_index("ix_reviews_status")
        batch_op.drop_index("ix_reviews_event_id")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("version")
        batch_op.drop_column("raw_score")
        batch_op.drop_column("status")
        batch_op.drop_column("rubric_id")
        batch_op.drop_column("event_id")

    op.drop_table("rubric_criteria")
    op.drop_table("rubrics")
    op.drop_table("judge_assignments")
    op.drop_table("judge_invitations")
    op.drop_table("judge_tracks")

    with op.batch_alter_table("judges") as batch_op:
        batch_op.drop_index("ix_judges_account_id")
        batch_op.drop_column("account_id")
