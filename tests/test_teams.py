import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.database import Base
from app.models import Event, TeamMembership
from app.services import (
    accept_team_invitation,
    create_account,
    create_event,
    create_team,
    create_team_invitation,
    revoke_team_invitation,
)


@pytest.fixture
def team_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_team_creation_and_captain_assignment(team_db):
    """Verify team creation creates membership with captain role."""
    creator = create_account(team_db, email="captain@test.local", password="Password123!", name="Captain Bob")
    event = create_event(
        team_db,
        event_id="evt_test_teams",
        name="Team Test Hackathon",
        submissions_close=datetime(2026, 12, 31, tzinfo=timezone.utc),
        min_team_size=1,
        max_team_size=3,
    )

    team = create_team(team_db, event_id="evt_test_teams", name="Alpha Wolves", creator_account=creator)
    assert team.name == "Alpha Wolves"
    assert team.captain_account_id == creator.id
    assert len(team.members) == 1
    assert team.members[0]["role"] == "captain"


def test_single_team_per_account_per_event_constraint(team_db):
    """Verify an account cannot join or create multiple teams in the same event."""
    user = create_account(team_db, email="solo@test.local", password="Password123!", name="Solo Dev")
    event = create_event(
        team_db,
        event_id="evt_single_team",
        name="Single Team Event",
        submissions_close=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    # First team creation succeeds
    create_team(team_db, event_id="evt_single_team", name="First Team", creator_account=user)

    # Second team creation by same user in same event fails at service level
    with pytest.raises(ValueError, match="already a member of team"):
        create_team(team_db, event_id="evt_single_team", name="Second Team", creator_account=user)

    # Verify DB-level constraint prevents duplicate membership
    with pytest.raises(IntegrityError):
        duplicate_membership = TeamMembership(
            id="tmb_dup",
            team_id="tm_other",
            account_id=user.id,
            event_id="evt_single_team",
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
        team_db.add(duplicate_membership)
        team_db.commit()
    team_db.rollback()


def test_team_invitation_and_capacity_limit(team_db):
    """Verify team invitation generation and strict capacity enforcement."""
    capt = create_account(team_db, email="capt@test.local", password="Password123!", name="Capt")
    p1 = create_account(team_db, email="p1@test.local", password="Password123!", name="Participant 1")
    p2 = create_account(team_db, email="p2@test.local", password="Password123!", name="Participant 2")
    p3 = create_account(team_db, email="p3@test.local", password="Password123!", name="Participant 3")

    event = create_event(
        team_db,
        event_id="evt_cap_test",
        name="Capacity Event",
        submissions_close=datetime(2026, 12, 31, tzinfo=timezone.utc),
        min_team_size=1,
        max_team_size=2,  # Maximum 2 members!
    )

    team = create_team(team_db, event_id="evt_cap_test", name="Small Duo", creator_account=capt)

    # Non-captain cannot invite
    with pytest.raises(ValueError, match="Only the team captain"):
        create_team_invitation(team_db, team_id=team.id, email=p1.email, requester=p1)

    # Captain invites p1
    inv1 = create_team_invitation(team_db, team_id=team.id, email=p1.email, requester=capt)
    assert inv1.status == "pending"

    # p1 accepts -> now team size is 2 (at capacity)
    m1 = accept_team_invitation(team_db, token=inv1.token, account=p1)
    assert m1.account_id == p1.id

    # Trying to invite when full fails
    with pytest.raises(ValueError, match="maximum capacity"):
        create_team_invitation(team_db, team_id=team.id, email=p2.email, requester=capt)

    # Invalid token fails
    with pytest.raises(ValueError, match="Invalid invitation token"):
        accept_team_invitation(team_db, token="nonexistent_token", account=p3)


def test_captain_invitation_revocation(team_db):
    """Verify captain can revoke invitations, non-captains cannot, and revoked invitations cannot be accepted."""
    capt = create_account(team_db, email="capt2@test.local", password="Password123!", name="Capt 2")
    member = create_account(team_db, email="member@test.local", password="Password123!", name="Member")
    outsider = create_account(team_db, email="outsider@test.local", password="Password123!", name="Outsider")

    event = create_event(
        team_db,
        event_id="evt_revoke_test",
        name="Revoke Test Event",
        submissions_close=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    team = create_team(team_db, event_id="evt_revoke_test", name="Revoke Team", creator_account=capt)

    # Captain invites member
    inv = create_team_invitation(team_db, team_id=team.id, email=member.email, requester=capt)
    assert inv.status == "pending"

    # Outsider cannot revoke
    with pytest.raises(ValueError, match="Only the team captain"):
        revoke_team_invitation(team_db, team_id=team.id, invitation_id=inv.id, requester=outsider)

    # Captain revokes invitation
    revoked_inv = revoke_team_invitation(team_db, team_id=team.id, invitation_id=inv.id, requester=capt)
    assert revoked_inv.status == "revoked"

    # Member attempts to accept revoked invitation -> fails
    with pytest.raises(ValueError, match="already been revoked"):
        accept_team_invitation(team_db, token=inv.token, account=member)

    # Cannot revoke again
    with pytest.raises(ValueError, match="Cannot revoke invitation with status 'revoked'"):
        revoke_team_invitation(team_db, team_id=team.id, invitation_id=inv.id, requester=capt)
