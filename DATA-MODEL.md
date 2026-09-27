# Data Model

This document outlines the implemented relational database schema for HackForge through Phase 5 (`T1` & `T2` release).

The schema is managed via Alembic migrations (`0001_foundation.py`, `0002_add_indexes.py`, `0003_phase2_core_workflows.py`, `0004_phase3_judging.py`, and `0005_phase4_normalization_and_results.py`) and backed by PostgreSQL 17.

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

### 8. `reviews` (Phase 3 Judging Upgraded)
Judge scoring records with OCC concurrency and rubric snapshotting.
- `event_id` (VARCHAR(100), PK, FK -> `events.id`)
- `project_id` (VARCHAR(100), PK, FK -> `projects.id`)
- `judge_id` (VARCHAR(100), PK, FK -> `judges.id`)
- `rubric_id` (VARCHAR(100), FK -> `rubrics.id`, nullable): Rubric version snapshot used for evaluation.
- `raw_score` (FLOAT, default 0.0): Composite score normalized to 0–100 range.
- `criteria` (JSON): Category scores mapping (e.g. `{"functionality": 5.0}`).
- `comment` (TEXT, default ""): Private judge feedback.
- `status` (VARCHAR(50), default "finalized"): `"draft"` or `"finalized"`.
- `version` (INT, default 1): Optimistic concurrency control version counter.
- `updated_at` (TIMESTAMPTZ, UTC): Timestamp of last evaluation write.

### 9. `rubrics`
Event scoring rubrics with version freezing upon completed reviews.
- `id` (VARCHAR(100), PK): Rubric identifier (e.g. `rub_...`).
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
- `name` (VARCHAR(200)): Rubric name.
- `version` (INT, default 1): Version counter for frozen historical snapshots.
- `is_active` (BOOLEAN, default True): Active flag for evaluations.
- `created_at` (TIMESTAMPTZ, UTC)
- `created_by_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable)

### 10. `rubric_criteria`
Individual weighted criteria within a rubric.
- `id` (VARCHAR(100), PK)
- `rubric_id` (VARCHAR(100), FK -> `rubrics.id`, ON DELETE CASCADE)
- `key` (VARCHAR(100)): Machine-readable key (e.g. `functionality`, `innovation`).
- `name` (VARCHAR(200)): Display label.
- `description` (TEXT): Evaluation guidance.
- `weight` (FLOAT, default 1.0): Positive weighting multiplier ($w_i > 0$).
- `min_score` (FLOAT, default 1.0): Minimum valid rating.
- `max_score` (FLOAT, default 5.0): Maximum valid rating.
- `sort_order` (INT, default 0): Display order in evaluation form.
- **Constraint**: `UniqueConstraint("rubric_id", "key")`.

### 11. `judge_invitations`
Cryptographic one-time invitations for prospective judges.
- `id` (VARCHAR(100), PK)
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
- `email` (VARCHAR(320), INDEX): Normalized recipient email address.
- `token_hash` (VARCHAR(64), UNIQUE, INDEX): SHA-256 hash of the 256-bit URL-safe invitation token.
- `status` (VARCHAR(50), default "pending"): `"pending"`, `"accepted"`, `"revoked"`, `"expired"`.
- `tracks` (JSON, nullable): Permitted track IDs list (`["*"]` explicitly permits all tracks; explicit track IDs permit only those tracks; `[]` or `null` on `Judge.tracks` permits no tracks).
- `expires_at` (TIMESTAMPTZ, UTC): Expiration deadline (default 7 days).
- `created_at` (TIMESTAMPTZ, UTC)
- `created_by_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable)
- `accepted_at` (TIMESTAMPTZ, UTC, nullable)
- `accepted_by_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable)

### 12. `judge_assignments`
Allocation of submitted projects to judges.
- `id` (VARCHAR(100), PK)
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
- `judge_id` (VARCHAR(100), FK -> `judges.id`, ON DELETE CASCADE)
- `project_id` (VARCHAR(100), FK -> `projects.id`, ON DELETE CASCADE)
- `assigned_at` (TIMESTAMPTZ, UTC)
- `assigned_by_account_id` (VARCHAR(100), FK -> `accounts.id`, nullable)
- **Constraint**: `UniqueConstraint("event_id", "judge_id", "project_id")`.

### 13. `review_criterion_scores`
Normalized relational scores per criterion for multi-judge reporting.
- `id` (VARCHAR(100), PK)
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE)
- `project_id` (VARCHAR(100), FK -> `projects.id`, ON DELETE CASCADE)
- `judge_id` (VARCHAR(100), FK -> `judges.id`, ON DELETE CASCADE)
- `criterion_id` (VARCHAR(100), FK -> `rubric_criteria.id`, ON DELETE CASCADE)
- `score` (FLOAT): Numeric rating.
- `created_at` (TIMESTAMPTZ, UTC)
- **Constraint**: `UniqueConstraint("event_id", "project_id", "judge_id", "criterion_id")`.

### 14. `judge_tracks`
Track restriction permissions for judges.
- `event_id` (VARCHAR(100), PK, FK -> `events.id`, ON DELETE CASCADE)
- `judge_id` (VARCHAR(100), PK, FK -> `judges.id`, ON DELETE CASCADE)
- `track_id` (VARCHAR(100), PK, FK -> `tracks.id`, ON DELETE CASCADE)

### 15. `results_snapshots` (Phase 4/5 Migration `0005`)
Immutable organizer results snapshots with self-contained v2 canonical calculation manifests.
- `id` (VARCHAR(100), PK): Snapshot identifier (e.g. `snp_evt_01_...`).
- `event_id` (VARCHAR(100), FK -> `events.id`, ON DELETE CASCADE, INDEX): Associated hackathon event (note: `events.target_reviews_per_project` INT default `3` was also added in migration `0005`).
- `rubric_id` (VARCHAR(100), FK -> `rubrics.id`, ON DELETE SET NULL, nullable): Active rubric at snapshot time.
- `created_by_account_id` (VARCHAR(100), FK -> `accounts.id`, ON DELETE SET NULL, nullable): Organizer who captured the snapshot.
- `created_at` (TIMESTAMPTZ, UTC): Snapshot creation timestamp.
- `method` (VARCHAR(50), default `"zscore_v1"`): Normalization algorithm identifier.
- `parameters` (JSON): Algorithm parameters (`min_sample`, `epsilon`, `target_reviews_per_project`, `cohort_policy`).
- `results_data` (JSON): Complete computed cohort rankings, judge calibration statistics, connectivity components, and excluded review diagnostics.
- `input_review_hashes` (JSON): Stores `manifest_version = 2`, `state_fingerprint` (SHA-256), `reviews` hash map, and the complete self-contained `manifest` captured under `REPEATABLE READ` isolation.


