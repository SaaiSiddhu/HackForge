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
|   - Security Headers Middleware                           |
|   - Route Handlers (HTML Views + Health Endpoints)        |
|   - Jinja2 Template Engine (Local HTML/CSS)               |
+-----------------------------------------------------------+
                              | Managed Sessions (get_db)
                              v
+-----------------------------------------------------------+
|                    SQLAlchemy 2 Layer                     |
|   - Synchronous Engine with Connection Pooling            |
|   - Declarative Mapped Models & Constraints               |
|   - PostgreSQL Advisory Locking for Seeding               |
+-----------------------------------------------------------+
                              | TCP (Internal Port 5432)
                              v
+-----------------------------------------------------------+
|                    PostgreSQL 17 Database                 |
|   - Persistent Named Volume (database)                    |
|   - Relational Schema with Foreign Keys & Indexes         |
+-----------------------------------------------------------+
```

---

## Component Boundaries

1. **Configuration (`app/config.py`)**:
   - Centralized, validated Pydantic model (`Settings`).
   - Validates `ENVIRONMENT`, `DATABASE_URL`, `LOG_LEVEL`, `FIXTURE_PATH`, `SEED_FIXTURES`, `HOST`, `PORT`.
   - Strict environment invariant: SQLite is allowed **strictly** when `ENVIRONMENT=testing`. In development and production, missing or SQLite URLs raise explicit startup validation errors.

2. **Database & Connection Pooling (`app/database.py`)**:
   - Synchronous SQLAlchemy 2 engine with `pool_pre_ping=True`, `pool_size=10`, `max_overflow=20`, and `pool_recycle=1800`.
   - FastAPI dependency `get_db()` owns transaction boundaries: yields session, commits on clean return, automatically rolls back on exception, and guarantees session closure in `finally`.
   - Dedicated `check_database_health()` probe executes lightweight `SELECT 1` without leaking connection secrets.

3. **Domain Models (`app/models.py`)**:
   - Clean 1:N and M:N relationships between `Event`, `Track`, `Team`, `Judge`, `Project`, and `Review`.
   - Explicit column constraints: primary keys, foreign keys, not-null constraints, and performance indexes on `projects(title)`, `projects(track_id)`, and `projects(team_id)`.
   - Model-level `safe_repo_url` property validating external links.

4. **Fixture Importer (`app/seed.py`)**:
   - Comprehensive two-phase importer: validates complete fixture structure and referential integrity **before** database writes.
   - Transactional insert-if-not-exists preserves existing records and local operator edits.
   - Concurrency safety: acquires PostgreSQL advisory lock `pg_advisory_xact_lock(0x4841434B)` during execution.
   - Non-destructive by default; explicit `--reset` flag provided for operator demo resets.

5. **Public Handlers & Observability (`app/main.py`)**:
   - Liveness probe (`/health/live`) confirms HTTP process health.
   - Readiness probe (`/health/ready`) verifies database connectivity (returns HTTP 503 on database degradation without internal tracebacks).
   - Public gallery (`/projects`) and detail (`/projects/{project_id}`).
   - SQL LIKE wildcard escaping (`%` and `_` autoescaped) ensures search input is treated strictly as literal data.
   - Custom friendly error handlers for HTTP 404 and 500.

6. **Static Assets & Templates (`app/static/`, `app/templates/`)**:
   - 100% locally served CSS and semantic HTML.
   - Zero external scripts, Google Fonts, or CDN dependencies.

---

## Operational Conventions

- **Timestamps**: Stored as timezone-aware UTC timestamps (`DateTime(timezone=True)`).
- **Concurrency**: Fast synchronous route handlers run in FastAPI's external worker threadpool (`anyio.to_thread.run_sync`), preventing event-loop starvation while avoiding the complexity of async ORM transactions.
- **Security Headers**: All responses include `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, and `Permissions-Policy`.
- **Private Data Protection**: Routes selecting project records join only `tracks` and `teams`; reviewer identities, emails, criteria scores, and comments are omitted from queries and template contexts.

---

## Phase 2 Architecture Handoff: Authentication & Access Control

Phase 2 will introduce user accounts, sessions, and role-based access control. The planned architecture is designed as follows:

1. **Accounts & Password Hashing**:
   - Dedicated `accounts` table: `id`, `email` (unique, lowercase), `password_hash`, `full_name`, `is_platform_admin`, `created_at`.
   - Password hashing using `pwdlib` / `passlib` with Argon2id or bcrypt.
   - Participant self-registration vs organizer-invited judge accounts.

2. **Session Management**:
   - Database-backed `sessions` table: `id` (cryptographically random token hash), `account_id`, `created_at`, `expires_at`, `revoked_at`.
   - Issued via secure HTTP cookies: `HttpOnly`, `SameSite=Lax`, `Secure` (conditional for local HTTP development).
   - Server-side revocability (logout invalidates session record).

3. **Event-Scoped Roles**:
   - `event_memberships` table: `(event_id, account_id, role)` where `role` in `('organizer', 'judge', 'participant')`.
   - Separation of platform administration (managing events) and event-scoped duties.

4. **CSRF Protection**:
   - Synchronized double-submit cookie or server-signed session CSRF token for all state-changing HTML form requests (POST/PUT/DELETE).

5. **Submission Lifecycle**:
   - `submissions` table: `event_id`, `team_id`, `title`, `summary`, `description`, `repo_url`, `is_final` (draft vs final), `submitted_at`.
   - Server-side deadline check: compare `now(UTC) <= event.submissions_close`. Late mutations rejected with HTTP 403/400.
