# Architecture

**HackForge** is structured as a modular monolith optimized for self-hosting reliability, verifiable correctness, and zero cloud lock-in.

---

## High-Level Topology

```
+-----------------------------------------------------------+
|                          Browser                          |
+-----------------------------------------------------------+
                              | HTTP (Port 8080)
                              v
+-----------------------------------------------------------+
|                     FastAPI Gateway                       |
|   - Security Headers & Synchronized CSRF Middleware       |
|   - Session Cookie & Bearer Token Authentication          |
|   - Static Asset Mount (/static) & Access-Controlled      |
|     Project Media (/projects/{id}/media/{filename})       |
|   - Jinja2 Template Engine (Offline Local HTML/CSS)       |
+-----------------------------------------------------------+
                              | Managed Sessions (get_db)
                              v
+-----------------------------------------------------------+
|                 Auth & Service Layer                      |
|   - Argon2id Password Hashing                             |
|   - SHA-256 Hashed Revocable Sessions                     |
|   - Concurrency-Safe Team Capacity (SELECT FOR UPDATE)    |
|   - Server-Side Submission Deadline & OCC Checks          |
|   - Pillow Image Verification & Re-encoding (<=5MB)       |
|   - Z-Score Cohort Normalization & v2 Results Snapshots   |
+-----------------------------------------------------------+
                              | TCP (Internal Port 5432)
                              v
+-----------------------------------------------------------+
|                    PostgreSQL 17 Database                 |
|   - Application Database: hackforge                       |
|   - Isolated Test Database: hackforge_test                |
|   - Named Volumes: database, uploads                      |
+-----------------------------------------------------------+
```

---

## Component Boundaries

1. **Configuration (`app/config.py`)**:
   - Centralized, validated Pydantic model (`Settings`).
   - Validates `ENVIRONMENT`, `DATABASE_URL`, `TEST_DATABASE_URL`, `LOG_LEVEL`, `FIXTURE_PATH`, `SEED_FIXTURES`, `COOKIE_SECURE`, `UPLOAD_DIR`, `HOST`, `PORT`.
   - Strict environment invariant: SQLite is allowed **strictly** when `ENVIRONMENT=testing`. In development and production, missing or non-PostgreSQL schemes raise explicit startup validation errors.
   - Strict boolean parser rejecting ambiguous inputs (`"maybe"`, `"null"`).
   - Sanitized URL formatting to prevent password leakage in logs or exceptions.

2. **Database & Connection Pooling (`app/database.py`)**:
   - Synchronous SQLAlchemy 2 engine with connection pooling (`pool_pre_ping=True`, `pool_size=10`, `max_overflow=20`, `pool_recycle=1800`).
   - Transaction ownership: `get_db()` yields managed sessions with rollback-on-exception, guaranteeing that caller services and HTTP handlers explicitly manage and commit transactions.
   - Dedicated `check_database_health()` probe executes lightweight `SELECT 1` without leaking connection secrets.

3. **Authentication & Session Security (`app/auth.py`)**:
   - **Argon2id Hashing**: High-memory, multi-threaded password hashing (`argon2-cffi`), requiring minimum 8 characters.
   - **Revocable Sessions**: 256-bit entropy raw tokens issued to clients, stored in the database exclusively as cryptographic SHA-256 hashes (`sessions` table). Token theft from the database is mitigated.
   - **Cookie Hardening**: `HttpOnly`, `SameSite=Lax`, `Path=/`, and configurable `Secure` flag via `COOKIE_SECURE`.
   - **CSRF Protection**: Double-submit cookie with token validation on mutating requests (`POST`, `PUT`, `DELETE`). Pure API Bearer tokens (`Authorization: Bearer <token>`) are supported for non-cookie API clients; ambiguous requests carrying both a session cookie and a conflicting Bearer token are rejected (`400 Bad Request`).

4. **Event & Team Management (`app/services.py`)**:
   - **Single Team Per Event**: Database unique constraint `UniqueConstraint("event_id", "account_id")` on `team_memberships` prevents participants from joining multiple teams in the same hackathon.
   - **Team Invitations**: Cryptographically secure 7-day invite tokens with concurrency-safe member capacity checks using PostgreSQL row-level locks (`SELECT ... FOR UPDATE`).
   - **Event Roles**: Explicit event-scoped authorization for Organizers, Judges, and Participants, with Platform Administrator overrides.
   - **Audit Logging**: Immutable event audit trail (`event_audit_logs`) tracking configuration changes, team creations, and project submissions.

5. **Project Submission Lifecycle (`app/services.py`)**:
   - **Draft vs Submitted**: Participants can iteratively save drafts (`status='draft'`), which increments the project version without exposing unfinished work to the public gallery.
   - **Deadline Enforcement**: Server-side checks verify current time against `event.submissions_open` and `event.submissions_close`. Submissions at or after the deadline (`now >= submissions_close`) are strictly rejected with `403 Forbidden`.
   - **Duplicate Project Protection**: Single submission per team enforced at service level for new participant submissions, while preserving existing historical fixture duplicates (`prj_07` and `prj_41`).

6. **Image Upload & Access-Controlled Media Pipeline (`app/services.py`, `app/main.py`)**:
   - Reads uploads in bounded 64 KB chunks (enforcing a strict 5 MB maximum before memory allocation).
   - Validates and re-encodes images using `Pillow` (`PNG`, `JPEG`, `GIF`, `WebP`, bounded to `4096x4096` pixels) plus leading magic-byte checks.
   - Random UUID filename allocation within `/portal/uploads/{project_id}/` prevents directory traversal and file collisions.
   - Served via access-controlled endpoint `GET /projects/{project_id}/media/{filename}`: draft media is restricted to team members, event organizers, and platform admins, while submitted project media is public.

7. **Fixture Importer & Seeder (`app/seed.py`)**:
   - Acquires PostgreSQL advisory transaction lock (`pg_try_advisory_xact_lock(0x4841434B)`) to eliminate race conditions during concurrent startup.
   - Timezone normalization: all timestamps parsed to UTC timezone-aware datetimes.
   - Referential integrity: validates judge track interests, teams, projects, and scores before committing.
   - Idempotent across restarts: preserves existing judge `account_id` bindings, unexpired demo sessions, and local edits; honors `SEED_FIXTURES=false`.

8. **Judging & Evaluation Subsystem (`app/judging_services.py`, `app/routers/judging.py`)**:
   - **Modular Routing**: Judging routes are isolated in `app/routers/judging.py` and included in the main FastAPI application via `app.include_router(judging_router)`. Shared template configuration resides in `app/templating.py`.
   - **Judge Invitation Lifecycle**: One-time invite links backed by cryptographic SHA-256 token hashes, enforcing explicit account association (`account_id`), track permissions (`["*"]` for all tracks, explicit track IDs, or `[]` for no tracks), and 7-day expiration.
   - **Weighted Scoring Rubrics**: Multi-criteria weighted rubrics with automatic version freezing when reviews exist to guarantee historical review immutability.
   - **Batch Assignment**: Deterministic, load-balanced judge assignment that strictly excludes conflicts of interest (team co-membership) and honors track permissions.
   - **Concurrency Control & Backend Isolation**: Review evaluations utilize `SELECT ... FOR UPDATE` with optimistic concurrency checking on `Review.version`. HTTP endpoints strictly deny peer review access, participant access, and dual-role organizer-judge leakage (`403 Forbidden`).

9. **Normalization, Results Snapshots & CSV Export Subsystem (`app/scoring_services.py`, `app/export_services.py`, `app/routers/results.py`)**:
   - **Comparable Cohort Z-Score Engine (`zscore_v1`)**: Computes per-judge population mean ($\mu_j$) and standard deviation ($\sigma_j = \text{pstdev}(S_j)$) within comparable `(event_id, track_id, rubric_id)` cohorts (plus an optional diagnostic cross-track pooling cohort `all:<rubric_id>`). Excludes judges with $N_j < 3$ or $\sigma_j \le 10^{-6}$ from normalized indices while preserving raw scores.
   - **Bipartite Connectivity Analysis**: Runs BFS across the usable judge-project graph and withholds unqualified global `normalized_rank` whenever a cohort has disconnected components (`component_count > 1`), reporting component-local ranks (`component_normalized_rank`).
   - **Consistent-Read Canonical v2 Snapshots**: Captures all calculation inputs in a single `REPEATABLE READ` transaction into an immutable v2 manifest (`results_snapshots`), enabling deterministic reproduction and staleness detection (`check_snapshot_staleness`).
   - **RFC 4180 CSV Exports**: Streams `judging.csv`, `reviews.csv`, `assignments.csv`, `teams.csv`, `projects.csv`, and `judges-calibration.csv` with UTF-8 encoding and spreadsheet formula-injection sanitization.

