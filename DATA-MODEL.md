# Data Model

This document distinguishes currently implemented Phase 1 entities from planned Phase 2–4 structures.

---

## Implemented Entities (Phase 1)

The Phase 1 schema is managed via Alembic migrations (`0001_foundation.py` and `0002_add_indexes.py`) and backed by PostgreSQL 17.

```
 +------------------------+          +------------------------+
 |         events         | 1      * |         tracks         |
 |------------------------|<---------|------------------------|
 | id: VARCHAR(100) [PK]  |          | id: VARCHAR(100) [PK]  |
 | name: VARCHAR(200)     |          | event_id: VARCHAR(100) |
 | submissions_close: UTC |          | name: VARCHAR(200)     |
 +------------------------+          +------------------------+
      | 1            | 1                          ^
      |              |                            |
      | *            | *                          | 1
 +------------------------+                       |
 |         teams          |                       |
 |------------------------|                       |
 | id: VARCHAR(100) [PK]  |                       |
 | event_id: VARCHAR(100) |                       |
 | name: VARCHAR(200)     |                       |
 | members: JSON          |                       |
 +------------------------+                       |
      ^ 1                                         |
      |                                           |
      | *                                         | *
 +------------------------------------------------------------+
 |                          projects                          |
 |------------------------------------------------------------|
 | id: VARCHAR(100) [PK]                                      |
 | event_id: VARCHAR(100) [FK -> events.id]                   |
 | team_id: VARCHAR(100) [FK -> teams.id]                     |
 | track_id: VARCHAR(100) [FK -> tracks.id]                   |
 | title: VARCHAR(200) [INDEX: ix_projects_title]             |
 | summary: TEXT                                              |
 | repo_url: TEXT                                             |
 | submitted_at: TIMESTAMPTZ [UTC]                            |
 +------------------------------------------------------------+
      ^ 1                                         ^ 1
      |                                           |
      | *                                         | *
 +------------------------------------------------------------+
 |                          reviews                           |
 |------------------------------------------------------------|
 | judge_id: VARCHAR(100) [PK, FK -> judges.id]               |
 | project_id: VARCHAR(100) [PK, FK -> projects.id]           |
 | criteria: JSON                                             |
 | comment: TEXT                                              |
 +------------------------------------------------------------+
      | *
      |
      v 1
 +------------------------+
 |         judges         |
 |------------------------|
 | id: VARCHAR(100) [PK]  |
 | event_id: VARCHAR(100) |
 | name: VARCHAR(200)     |
 | email: VARCHAR(320)    |
 | tracks: JSON           |
 +------------------------+
```

### Table Definitions

1. **`events`**:
   - `id`: Primary key string (e.g., `evt_01`).
   - `name`: Human-readable event title.
   - `submissions_close`: UTC timestamp when submission edits close (`2026-03-01T18:00:00Z`).

2. **`tracks`**:
   - `id`: Primary key string (e.g., `trk_01`).
   - `event_id`: Foreign key referencing `events.id`.
   - `name`: Category name (e.g., `Developer tools`, `Security`, `Climate`).

3. **`teams`**:
   - `id`: Primary key string (e.g., `tm_01`).
   - `event_id`: Foreign key referencing `events.id`.
   - `name`: Team name.
   - `members`: JSON array storing fixture participant email strings.

4. **`judges`**:
   - `id`: Primary key string (e.g., `jdg_01`).
   - `event_id`: Foreign key referencing `events.id`.
   - `name`: Full name.
   - `email`: Contact email address.
   - `tracks`: JSON array storing assigned track IDs.

5. **`projects`**:
   - `id`: Primary key string (e.g., `prj_01`). Preserves all 41 distinct fixture records, including duplicate project title `Dry Harbour` (`prj_07` and `prj_41`).
   - `event_id`: Foreign key referencing `events.id`.
   - `team_id`: Foreign key referencing `teams.id`.
   - `track_id`: Foreign key referencing `tracks.id`.
   - `title`: Project title.
   - `summary`: Short description.
   - `repo_url`: Source repository URL.
   - `submitted_at`: UTC submission timestamp.
   - **Indexes**: `ix_projects_title`, `ix_projects_track_id`, `ix_projects_team_id`.

6. **`reviews`**:
   - `judge_id` & `project_id`: Composite primary key referencing `judges.id` and `projects.id`.
   - `criteria`: JSON dictionary of rubric criterion ratings.
   - `comment`: Reviewer comments.

---

## Important Architectural Notes on JSON Fields

In Phase 1, `teams.members`, `judges.tracks`, and `reviews.criteria` store raw JSON structures imported directly from `fixtures.json`.

> **CRITICAL**: These JSON fields are **purely import storage**. They are **not** an improvised authentication system, permission check, or dynamic scoring engine. No authorization or security logic relies on querying these JSON blobs.

---

## Planned Normalization Roadmap (Phase 2 & Phase 3)

The following tables will be introduced in subsequent migrations to replace import JSON with normalized relational models:

1. **`accounts` (Phase 2)**:
   - `id`: UUID primary key.
   - `email`: Lowercase, unique string.
   - `password_hash`: Secure argon2id or bcrypt hash.
   - `full_name`: Display name.
   - `is_platform_admin`: Boolean flag for platform administration.

2. **`sessions` (Phase 2)**:
   - `id`: Hash of cryptographically random 256-bit session token.
   - `account_id`: FK to `accounts.id`.
   - `created_at`: UTC timestamp.
   - `expires_at`: UTC expiration timestamp.
   - `revoked_at`: Nullable UTC revocation timestamp.

3. **`event_roles` (Phase 2)**:
   - Composite PK: `(event_id, account_id, role)`.
   - Supported roles: `organizer`, `judge`, `participant`.

4. **`team_memberships` (Phase 2)**:
   - Normalizes `teams.members` into relational records linking `accounts` to `teams` via invitation tokens.

5. **`rubrics` & `rubric_criteria` (Phase 3)**:
   - Normalizes `reviews.criteria` into configured scoring dimensions: `id`, `event_id`, `name`, `weight` (float), `min_score` (int), `max_score` (int).

6. **`judge_assignments` (Phase 3)**:
   - Explicit assignment mapping `(event_id, judge_id, project_id)` enforcing judge isolation.

---

## Cross-Event Invariants

The data model enforces the following relational invariants:

1. **Project Scope Invariant**: A project must belong to the same event as both its team and its track:
   $$\text{project.event\_id} = \text{team.event\_id} = \text{track.event\_id}$$
2. **Review Scope Invariant**: A review must only connect a judge and a project registered in the same event:
   $$\text{judge.event\_id} = \text{project.event\_id}$$
3. **Immutability of Closed Submissions**: Projects with $\text{event.submissions\_close} < \text{now(UTC)}$ reject modification.
