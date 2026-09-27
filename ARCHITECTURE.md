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
|   - CSRF & Session Cookie Validation                      |
|   - Static & Local Upload Mounts (/static, /uploads)      |
|   - Jinja2 Template Engine (Local HTML/CSS)               |
+-----------------------------------------------------------+
                              | Managed Sessions (get_db)
                              v
+-----------------------------------------------------------+
|                 Auth & Service Layer                      |
|   - Argon2id Password Hashing                             |
|   - SHA-256 Hashed Revocable Sessions                     |
|   - Concurrency-Safe Team Capacity (SELECT FOR UPDATE)    |
|   - Server-Side Submission Deadline Checks                |
|   - Magic Byte Image Validation (JPEG/PNG/GIF/WebP, <=5MB)|
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
   - **CSRF Protection**: Double-submit cookie with token validation on mutating form requests (`POST`, `PUT`, `DELETE`). JSON API requests verify headers or appropriate API authentication.

4. **Event & Team Management (`app/services.py`)**:
   - **Single Team Per Event**: Database unique constraint `UniqueConstraint("event_id", "account_id")` on `team_memberships` prevents participants from joining multiple teams in the same hackathon.
   - **Team Invitations**: Cryptographically secure 7-day invite tokens with concurrency-safe member capacity checks using PostgreSQL row-level locks (`SELECT ... FOR UPDATE`).
   - **Event Roles**: Explicit event-scoped authorization for Organizers, Judges, and Participants, with Platform Administrator overrides.
   - **Audit Logging**: Immutable event audit trail (`event_audit_logs`) tracking configuration changes, team creations, and project submissions.

5. **Project Submission Lifecycle (`app/services.py`)**:
   - **Draft vs Submitted**: Participants can iteratively save drafts (`status='draft'`), which increments the project version without exposing unfinished work to the public gallery.
   - **Deadline Enforcement**: Server-side checks verify current time against `event.submissions_open` and `event.submissions_close`. Submissions after deadline are strictly rejected with `403 Forbidden`.
   - **Duplicate Project Protection**: Single submission per team enforced at service level for new participant submissions, while preserving existing historical fixture duplicates (`prj_07` and `prj_41`).

6. **Image Upload Pipeline (`app/services.py`)**:
   - Validates file content using leading magic bytes:
     - JPEG: `b"\xFF\xD8\xFF"`
     - PNG: `b"\x89PNG\r\n\x1a\n"`
     - GIF: `b"GIF87a"` / `b"GIF89a"`
     - WebP: `b"RIFF" ... b"WEBP"`
   - Size limit: 5 MB (5,242,880 bytes).
   - Random UUID filename allocation prevents directory traversal and file collisions.
   - Persisted to persistent Docker volume `uploads` and served via `/uploads/{filename}`.

7. **Fixture Importer & Seeder (`app/seed.py`)**:
   - Acquires PostgreSQL advisory transaction lock (`pg_try_advisory_xact_lock(0x4841434B)`) to eliminate race conditions during concurrent startup.
   - Timezone normalization: all timestamps parsed to UTC timezone-aware datetimes.
   - Referential integrity: validates judge track interests against registered event tracks.
   - Safeguards `--reset` to prevent accidental data loss in production environments.
