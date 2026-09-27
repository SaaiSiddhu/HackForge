# Phase 1 Verification Record: Foundation

**Date of Execution**: September 27, 2026  
**Platform**: HackForge — Build. Submit. Judge.  
**Target Milestone**: Phase 1 Foundation Completion (DOGFOOD 2026)  
**Submission Deadline**: September 28, 2026, 18:00 UTC / 11:30 PM IST (~35 hours remaining)

---

## 1. What Was Implemented

1. **Validated Configuration Subsystem (`app/config.py`)**:
   - Pydantic-based `Settings` model validating environment variables on startup.
   - Enforces PostgreSQL for development and production environments; rejects accidental SQLite fallbacks. SQLite is strictly permitted under `ENVIRONMENT=testing`.
   - `.env.example` provided with safe local defaults. Local `.env` files excluded from Git and Docker build contexts.

2. **SQLAlchemy 2 & Alembic Migration Foundation (`app/database.py`, `migrations/`)**:
   - Connection pooling with pre-ping validation, max overflow limits, and automatic recycling.
   - Session lifecycle managed in `get_db()` with commit on success, rollback on exceptions, and guaranteed session release.
   - Historical migrations `0001_foundation.py` and `0002_add_indexes.py` cleanly migrate empty PostgreSQL databases idempotently.

3. **Domain Models & Constraints (`app/models.py`)**:
   - Relational definitions for `Event`, `Track`, `Team`, `Judge`, `Project`, and `Review`.
   - Explicit performance indexes on `projects(title)`, `projects(track_id)`, and `projects(team_id)`.
   - `safe_repo_url` validation stripping non-http/https external schemes (`javascript:`, `data:`).

4. **Correct & Repeatable Fixture Importer (`app/seed.py`)**:
   - Pre-validation of complete fixture schema and referential integrity before committing any writes.
   - Idempotent insert-if-not-exists preserves existing records and local operator edits.
   - Preserves all 41 projects, including the deliberate duplicate title `Dry Harbour` (`prj_07` and `prj_41`) with distinct primary keys.
   - Preserves missing review distributions (projects have between 2 and 5 reviews) and constant-score cases (`jdg_01`, `jdg_07`).
   - Transactional safety: invalid input rolls back cleanly without partial writes.
   - Concurrency safety: PostgreSQL transaction-level advisory locks (`0x4841434B`).
   - Explicit `--reset` flag for intentional operator demo resets.

5. **Public Gallery & Project Detail (`app/main.py`, `app/templates/`, `app/static/style.css`)**:
   - Responsive dark-theme UI with lime accents (`#b9f56a`) using local CSS and system fonts.
   - Case-insensitive search with autoescaped SQL LIKE wildcards (`%` and `_` treated as literal data).
   - Track filtering and combined search + filter with deterministic ordering (`title ASC, id ASC`).
   - Accessible form controls with explicit labels and styled empty states.
   - Zero leak of reviewer emails, judge tracks, criteria scores, or private comments.
   - Custom friendly HTML error pages for 404 and 500 without stack trace leaks.
   - Security headers middleware (`nosniff`, `DENY`, `strict-origin-when-cross-origin`).

6. **Observability Probes**:
   - `/health/live`: Liveness probe (HTTP process status).
   - `/health/ready`: Readiness probe verifying PostgreSQL connectivity (returns 503 on database unavailability).
   - `/health`: Backward-compatible readiness alias.

---

## 2. What Remains Incomplete (Honest Phase 1 Scope)

The following workflows belong to subsequent phases and are **not** claimed or implemented in Phase 1:
- User accounts and password hashing (Phase 2).
- Cookie-based revocable sessions and CSRF protection (Phase 2).
- Event-scoped authorization: Participant, Judge, Organizer, Admin (Phase 2).
- Team creation and member invitations (Phase 2).
- Project draft/final submission writes and server-side submission deadline enforcement (Phase 2).
- Judge assignments, scoring forms, and backend-enforced peer score isolation (Phase 3).
- Cross-judge Z-score normalization and CSV exports (Phase 4).

Accordingly, `.dogfood.toml` explicitly declares `claimed = []`.

---

## 3. Startup & Testing Instructions

### Start Services
```sh
docker compose up -d
```
The application will wait for PostgreSQL health, execute Alembic migrations to head, load official fixtures, and start serving at `http://localhost:8080`.

### Health Check Verification
```sh
curl -i http://localhost:8080/health/ready
```

### Run Automated Test Suite
```sh
docker compose exec app python -m pytest -v
```

### Run Official DOGFOOD Acceptance Suite
```sh
python3 run.py .dogfood.toml
```

---

## 4. Actual Automated Test Results

Executed inside container environment (`Python 3.12.14`, `pytest 8.3.5`):

```
============================= test session starts ==============================
platform linux -- Python 3.12.14, pytest-8.3.5, pluggy-1.6.0 -- /usr/local/bin/python
cachedir: /tmp/.pytest_cache
rootdir: /portal
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.15.1
collected 33 items

tests/test_config.py::test_settings_valid_defaults PASSED                [  3%]
tests/test_config.py::test_settings_rejects_sqlite_in_production PASSED  [  6%]
tests/test_config.py::test_settings_rejects_sqlite_in_development PASSED [  9%]
tests/test_config.py::test_settings_requires_database_url_in_production PASSED [ 12%]
tests/test_config.py::test_settings_parses_boolean_seed_fixtures PASSED  [ 15%]
tests/test_foundation.py::test_seed_preserves_all_records_and_local_edits PASSED [ 18%]
tests/test_foundation.py::test_gallery_filters_and_does_not_leak_private_data PASSED [ 21%]
tests/test_health.py::test_liveness_probe PASSED                         [ 24%]
tests/test_health.py::test_readiness_probe_healthy PASSED                [ 27%]
tests/test_health.py::test_readiness_probe_unhealthy PASSED              [ 30%]
tests/test_health.py::test_health_alias PASSED                           [ 33%]
tests/test_migrations.py::test_sqlite_clean_migration_cycle PASSED       [ 36%]
tests/test_migrations.py::test_postgres_migration_idempotence PASSED     [ 39%]
tests/test_postgres_integration.py::test_postgres_foreign_key_enforcement PASSED [ 42%]
tests/test_postgres_integration.py::test_postgres_review_composite_pk_uniqueness PASSED [ 45%]
tests/test_routes.py::test_root_redirects_to_projects PASSED             [ 48%]
tests/test_routes.py::test_gallery_renders_known_fixture_titles PASSED   [ 51%]
tests/test_routes.py::test_gallery_search_case_insensitive PASSED        [ 54%]
tests/test_routes.py::test_gallery_search_escapes_sql_wildcards PASSED   [ 57%]
tests/test_routes.py::test_gallery_track_filtering PASSED                [ 60%]
tests/test_routes.py::test_gallery_combined_search_and_track_filter PASSED [ 63%]
tests/test_routes.py::test_gallery_empty_search_results PASSED           [ 66%]
tests/test_routes.py::test_project_detail_success PASSED                 [ 69%]
tests/test_routes.py::test_project_detail_not_found PASSED               [ 72%]
tests/test_routes.py::test_unsafe_repo_url_is_not_rendered_as_link PASSED [ 75%]
tests/test_routes.py::test_privacy_no_private_judging_data_leak PASSED   [ 78%]
tests/test_routes.py::test_security_headers_present PASSED               [ 81%]
tests/test_seed.py::test_seed_preserves_derived_counts_and_all_records PASSED [ 84%]
tests/test_seed.py::test_seed_preserves_closed_deadline_and_constant_scores PASSED [ 87%]
tests/test_seed.py::test_reseed_is_idempotent_and_preserves_local_edits PASSED [ 90%]
tests/test_seed.py::test_invalid_input_fails_validation_and_leaves_no_partial_writes PASSED [ 93%]
tests/test_seed.py::test_validation_detects_contradictory_duplicate_project_ids PASSED [ 96%]
tests/test_seed.py::test_operator_reset_reloads_fresh_data PASSED        [100%]

======================== 33 passed, 1 warning in 1.69s =========================
```

---

## 5. Official Acceptance Checker Result

Running `python3 run.py .dogfood.toml`:

```
DOGFOOD 2026 acceptance report
portal: http://localhost:8080
claimed: nothing
fixtures: fixtures.json

T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... FAIL
       GET http://localhost:8080
       sent as judge_b; this is the url that returns judge_a's scores
       got 200, wanted 401 or 403
       the backend returned another judge's scores
T2  participant blocked ............... FAIL
       GET http://localhost:8080
       sent as participant
       got 200, wanted 401 or 403
T2  csv export works .................. FAIL
       GET http://localhost:8080
       sent as organizer
       got 200 but the first line has no comma in it

claimed nothing, verified T1
```

> **Evaluation**: T1 passed basic public checks, but per hackathon rules and senior engineering discipline, **T1 is NOT claimed** until full user authentication, event creation, team invites, and project editing workflows are complete.

---

## 6. Official Files Verification

Verified against https://dogfoodhack.com/spec/ on September 27, 2026:

| File | SHA-256 Checksum | Match Status |
| :--- | :--- | :--- |
| `fixtures.json` | `252896bc45d49fca69ad413be40c6bfde9d9b9f9dd8db702b3ff74eaaa181121` | Exact match |
| `run.py` | `aa98963841bc8e18e8e5d76f0499697c093dd3c0055f9d73a459f592f4dcf09d` | Exact match |
| `docs/official-spec.md` | `644b92eb50a37215cb992589e451850ab95cb14803bbad3905fd8b071bfbd696` | Exact match |

Database verification confirmed exact counts from `fixtures.json`:
- **1 Event**: `evt_01` (deadline: `2026-03-01T18:00:00Z`).
- **8 Tracks**: All 8 tracks preserved.
- **40 Teams**: All 40 teams preserved.
- **30 Judges**: All 30 judges preserved.
- **41 Projects**: Both `prj_07` and `prj_41` (title: `Dry Harbour`) preserved.
- **126 Reviews**: Missing reviews and constant scoring patterns intact.

---

## 7. Persistence & Operational Integrity Verification

- **Volume Persistence**: Data resides in the named PostgreSQL volume `database`.
- **Restart Verification**:
  1. A project summary was modified in PostgreSQL.
  2. The application container was restarted via `docker compose restart app`.
  3. The local edit persisted exactly, counts remained unchanged (41 projects, 126 reviews), and zero duplicate records were created.
- **Offline Runtime**:
  - Image builds download base Python images and pip dependencies during preparation.
  - Runtime execution (`docker compose up`) operates 100% offline. Zero network calls to external APIs, CDNs, or telemetry servers.

---

## 8. Dependency & Security Baseline Audit

- **Dependencies**:
  - `fastapi==0.115.12`: Latest stable release; patched against denial of service and parsing vulnerabilities.
  - `SQLAlchemy==2.0.40`: Modern 2.0 ORM with parameterized query construction.
  - `psycopg[binary]==3.2.6`: Pure C-driver binary with prepared statements.
  - `Jinja2==3.1.6`: Patched against CVE-2024-56326 and CVE-2024-56201.
  - `alembic==1.15.2`: Deterministic schema versioning.
  - `uvicorn==0.34.2`, `pytest==8.3.5`, `httpx==0.28.1`.
- **Audit Findings**: Zero known high or critical CVEs in pinned versions.
- **Non-Root Execution**: Container runs under user `portal` (UID 1000). Host directories are not run as root.
- **Exposure**: Database port 5432 is internal only (not published to the host). HTTP binds to `127.0.0.1:8080`.

---

## 9. Phase 2 Implementation Checklist

Concrete handoff plan for implementing T1 (Core Hackathon Platform):

- [ ] **Accounts Table & Password Hashing**:
  - Migration adding `accounts` table (`id`, `email`, `password_hash`, `full_name`, `created_at`).
  - Implement Argon2id password hashing helper (`app/security.py`).
- [ ] **Session Subsystem**:
  - Migration adding `sessions` table (`id`, `account_id`, `expires_at`, `revoked_at`).
  - Implement cookie helper (`set_cookie(session_id, httponly=True, samesite="lax")`).
  - Auth dependency `get_current_user` in `app/dependencies.py`.
- [ ] **Event-Scoped Roles**:
  - Migration adding `event_memberships` (`event_id`, `account_id`, `role`).
  - Self-registration for participants (`/register`).
  - Organizer event creation and administration screens.
- [ ] **CSRF Protection**:
  - CSRF token validation on POST/PUT mutations.
- [ ] **Team Formation**:
  - Migration for `team_memberships` and invitation token generation.
  - Route `/teams/join/{token}` for invite-based onboarding.
- [ ] **Submissions Lifecycle & Server-Side Enforcement**:
  - Draft vs final project submission states.
  - Server-side deadline check against `event.submissions_close`. Reject mutations after deadline with HTTP 403.
