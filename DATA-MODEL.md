# Data Model

This document outlines the implemented relational database schema for HackForge through Phase 2.

The schema is managed via Alembic migrations (`0001_foundation.py`, `0002_add_indexes.py`, and `0003_phase2_core_workflows.py`) and backed by PostgreSQL 17.

---

## Entity Relationship Overview

```
                      +-----------------------------+
                      |          accounts           |
                      |-----------------------------|
                      | id: VARCHAR(100) [PK]       |
                      | email: VARCHAR(320) [UQ,IX] |
                      | password_hash: VARCHAR(255) |
                      | name: VARCHAR(200)          |
                      | is_platform_admin: BOOLEAN  |
                      | created_at: TIMESTAMPTZ     |
                      +-----------------------------+
                        | 1          | 1          | 1
                        |            |            |
                        | *          | *          | *
        +---------------+            |            +-------------------+
        |                            |                                |
        v                            v                                v
+-----------------------+  +--------------------+         +-----------------------+
|       sessions        |  |    event_roles     |         |   team_memberships    |
|-----------------------|  |--------------------|         |-----------------------|
| id: VARCHAR(100) [PK] |  | id: VARCHAR [PK]   |         | id: VARCHAR(100) [PK] |
| token_hash: STR [UQ]  |  | event_id: FK       |         | team_id: FK -> teams  |
| account_id: FK        |  | account_id: FK     |         | account_id: FK        |
| expires_at: TIMESTAMP |  | role: VARCHAR(50)  |         | event_id: FK          |
| created_at: TIMESTAMP |  | UQ(evt, acc, role) |         | role: captain/member  |
+-----------------------+  +--------------------+         | UQ(event_id, account) |
                                       |                  +-----------------------+
                                       |                               |
                                       v                               v
+---------------------------------------------------+     +-----------------------+
|                      events                       |     |         teams         |
|---------------------------------------------------|     |-----------------------|
| id: VARCHAR(100) [PK]                             | 1 * | id: VARCHAR(100) [PK] |
| name: VARCHAR(200)                                |<----| event_id: FK -> events|
| description: TEXT                                 |     | name: VARCHAR(200)    |
| registration_open / close: TIMESTAMPTZ            |     | members: JSON         |
| submissions_open / close: TIMESTAMPTZ             |     | captain_account_id: FK|
| min_team_size / max_team_size: INT                |     +-----------------------+
| prizes: JSON                                      |                 ^ 1
| created_by_account_id: FK                         |                 |
+---------------------------------------------------+                 |
  | 1               | 1                  | 1                          | *
  |                 |                    |                 +----------------------+
  v *               v *                  v *               |   team_invitations   |
+-------------+  +-----------------+  +-----------------+  |----------------------|
|   tracks    |  | event_questions |  |event_audit_logs |  | id: VARCHAR(100) [PK]|
|-------------|  |-----------------|  |-----------------|  | team_id: FK -> teams |
| id: VARCHAR |  | id: VARCHAR[PK] |  | id: VARCHAR[PK] |  | email: VARCHAR(320)  |
| event_id: FK|  | event_id: FK    |  | event_id: FK    |  | token: VARCHAR(64)   |
| name: STR   |  | label: VARCHAR  |  | account_id: FK  |  | status: pending/acc  |
+-------------+  | type: VARCHAR   |  | action: VARCHAR |  | expires_at: TIMESTAMP|
                 | required: BOOL  |  | payload: JSON   |  +----------------------+
                 +-----------------+  +-----------------+
```

---

## Entity Details

### 1. `accounts`
Stores platform user identity with Argon2id password hashes.
- `id` (VARCHAR(100), PK): Account identifier (e.g. `acc_...`).
- `email` (VARCHAR(320), UNIQUE, INDEX): Normalized lowercase user email address.
- `password_hash` (VARCHAR(255)): Argon2id password hash string.
- `name` (VARCHAR(200)): Display name.
- `is_platform_admin` (BOOLEAN): Platform-wide superadmin privilege flag.
- `created_at` (TIMESTAMPTZ, UTC): Account registration timestamp.

### 2. `sessions`
High-entropy, revocable user sessions.
- `id` (VARCHAR(100), PK): Session identifier.
- `token_hash` (VARCHAR(64), UNIQUE, INDEX): SHA-256 hash of the 256-bit URL-safe token. The raw token is stored only in the client cookie.
- `account_id` (VARCHAR(100), FK -> `accounts.id`, ON DELETE CASCADE): Session owner.
- `expires_at` (TIMESTAMPTZ, UTC): Expiration date (default 7 days).
- `created_at` (TIMESTAMPTZ, UTC): Creation timestamp.

### 3. `event_roles`
Event-scoped role permissions.
- `id` (VARCHAR(100), PK)
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
- `account_id` (VARCHAR(100), FK -> `accounts.id`, ON DELETE CASCADE)
- `role` (VARCHAR(50)): Role name (`"organizer"`, `"judge"`, `"participant"`).
- `created_at` (TIMESTAMPTZ, UTC)
- **Constraint**: `UniqueConstraint("event_id", "account_id", "role")`.

### 4. `events`
Hackathon configuration, schedule, and team sizing rules.
- `id` (VARCHAR(100), PK): Event identifier (e.g. `evt_01`, `evt_open_demo`).
- `name` (VARCHAR(200)): Hackathon name.
- `description` (TEXT): Public overview of the event.
- `registration_open`, `registration_close` (TIMESTAMPTZ, nullable): Registration window.
- `submissions_open`, `submissions_close` (TIMESTAMPTZ): Submission window. Server rejects late submissions when `now > submissions_close`.
- `min_team_size` (INT, default 1), `max_team_size` (INT, default 5): Capacity constraints.
- `prizes` (JSON): Prize structure list.
- `created_by_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable).

### 5. `teams` and `team_memberships`
Teams and membership rosters.
- `teams.id` (VARCHAR(100), PK): Team identifier (e.g. `tm_01`).
- `teams.captain_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable): Designated team captain.
- `team_memberships`:
  - `id` (VARCHAR(100), PK)
  - `team_id` (VARCHAR(100), FK -> `teams.id`, ON DELETE CASCADE)
  - `account_id` (VARCHAR(100), FK -> `accounts.id`, ON DELETE CASCADE)
  - `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
  - `role` (VARCHAR(50)): `"captain"` or `"member"`.
  - `joined_at` (TIMESTAMPTZ, UTC)
  - **Single Team Constraint**: `UniqueConstraint("event_id", "account_id")` enforces at the database level that an account belongs to at most one team per event.

### 6. `team_invitations`
Invitations to join a team.
- `id` (VARCHAR(100), PK)
- `team_id` (VARCHAR(100), FK -> `teams.id`, ON DELETE CASCADE)
- `email` (VARCHAR(320), INDEX): Recipient email.
- `token` (VARCHAR(64), UNIQUE, INDEX): Secret invite URL token.
- `status` (VARCHAR(50)): `"pending"`, `"accepted"`, `"declined"`, `"expired"`.
- `expires_at` (TIMESTAMPTZ, UTC): 7-day expiration.

### 7. `projects`
Project submissions and drafts.
- `id` (VARCHAR(100), PK): Project identifier.
- `event_id` (VARCHAR(100), FK -> `events.id`)
- `team_id` (VARCHAR(100), FK -> `teams.id`): Deliberately non-unique at DB level to preserve historical duplicate submissions `prj_07` and `prj_41`. Single project per team is enforced at the service layer for new participant submissions.
- `track_id` (VARCHAR(100), FK -> `tracks.id`)
- `title` (VARCHAR(200), INDEX): Project name.
- `tagline` (VARCHAR(255)): One-line summary pitch.
- `summary` (TEXT): High-level overview.
- `description` (TEXT): Full architecture/markdown writeup.
- `repo_url` (TEXT): Source code repository URL.
- `demo_url` (TEXT): Live deployment URL.
- `video_url` (TEXT): Walkthrough video URL.
- `status` (VARCHAR(50), INDEX): `"draft"` or `"submitted"`. Public gallery filters strictly for `"submitted"`.
- `thumbnail_url` (TEXT): Local upload URL (e.g. `/uploads/abc.png`).
- `version` (INT): Incremented on each update.
- `submitted_at` (TIMESTAMPTZ, UTC): Final submission timestamp.
- `updated_at` (TIMESTAMPTZ, UTC): Last draft update timestamp.

### 8. `reviews` (Phase 1 Baseline Preserved)
Judge scoring records.
- `judge_id` (VARCHAR(100), PK, FK -> `judges.id`)
- `project_id` (VARCHAR(100), PK, FK -> `projects.id`)
- `criteria` (JSON): Category scores (e.g. `{"functionality": 5}`).
- `comment` (TEXT): Private feedback.
