# HackForge

**Build. Submit. Judge.** A self-hosted hackathon submission and judging platform for DOGFOOD 2026.

## Canonical Workspace
- **Canonical Repository Path**: `/Users/saai_siddharth/Projects/Hackathon/HackForge` (Single source of truth).
- **Architecture**: FastAPI (Python 3.12), PostgreSQL 17, SQLAlchemy 2.0, Alembic, Jinja2 SSR, local filesystem uploads.

---

## Milestone Status: Phase 2 (T1 Core Workflows Verified & Hardened)

Phase 2 establishes full T1 core workflows on top of a hardened, audited Phase 1 foundation and comprehensive Phase 2 repair pass:

- **Phase 2 Repair & Verification Pass Completed**:
  - **First-Visit CSRF Synchronized**: A single CSRF token is generated per request and synchronized between the response cookie and rendered HTML forms. Converted `/logout` to a CSRF-protected `POST` endpoint; `GET /logout` returns 405 Method Not Allowed.
  - **Authentication & API CSRF Enforcement**: Removed blanket JSON CSRF exemption. Any request presenting session cookies requires valid CSRF tokens. Pure Bearer tokens (`Authorization: Bearer <token>`) are supported for API clients and automated checkers without CSRF tokens. Ambiguous requests carrying both session cookies and conflicting Bearer tokens are rejected with `HTTP 400 Bad Request`.
  - **Submission & Upload Authorization Closed**: Submissions strictly validate that the requested `project_id` matches the user's team and event. Server-side deadline enforcement operates inclusively (`now >= submissions_close`). Cross-team project modifications and cross-event submissions are rejected.
  - **Protected Draft Media & Pillow Validation**: Replaced unconditional `/uploads` static mount with access-controlled route `GET /projects/{project_id}/media/{filename}`. Draft media is visible only to team members, event organizers, and platform admins; once submitted, media is publicly accessible. Images are verified using `Pillow`, restricted to valid formats (PNG, JPEG, GIF, WebP), constrained to maximum 4096x4096px bounds, safely re-encoded, and read in 64KB bounded chunks (max 5MB). Docker volume permissions are configured in Dockerfile (`chown portal:portal`) without manual chmod 777.
  - **Optimistic Concurrency Control**: Project updates enforce `expected_version`. Stale updates return `HTTP 409 Conflict`, verified across both SQLite and PostgreSQL.
  - **Complete T1 Organizer Workflow**: Added `GET/POST /events/new` route and UI. Validates chronological date ordering (`reg_open <= reg_close`, `sub_open <= sub_close`). Supports track creation, prize definitions, and custom event questions. Required custom questions are validated on final submission. Added captain invitation revocation (`POST /teams/{id}/invitations/{inv_id}/revoke`). All administrative actions commit atomic audit logs (`event_audit_logs`).
  - **Explicit Demo Provisioning**: Gated behind `ENABLE_DEMO_PROVISIONING=true`; strictly refused in `production`. Session tokens stored exclusively as SHA-256 hashes.
  - **Complete Test Isolation**: Tests override `get_db` and temporary `upload_dir` via monkeypatching. Normal application data and uploads are untouched. Migrations and integration tests run against isolated `hackforge_test` database.
  - **Honest Tooling Disclosure**: Headless browser automation tools (Playwright/Puppeteer) are not available in this CLI agent environment. End-to-end user journeys are verified via comprehensive HTTP integration tests with fresh cookie jars (`tests/test_browser_flow.py`).
  - **Official Dogfood Acceptance Checker**: Unmodified `run.py .dogfood.toml` passes 100% for T1 (`claimed T1, verified T1`). Zero T2 claims.

---

## Quickstart

### Prerequisites
- Docker Engine 24+ and Docker Compose v2.

### 1. Start the Platform
```sh
docker compose up -d
```
The application starts PostgreSQL, runs Alembic migrations to head (`0003`), loads official fixtures and test logins, and listens on:
[http://localhost:8080](http://localhost:8080)

### 2. Pre-Seeded Test Accounts & Sessions
The environment is pre-configured with test logins matching `.dogfood.toml`:
| Role | Email | Password | Auth Header |
|---|---|---|---|
| **Organizer / Admin** | `organizer@hackforge.local` | `Password123!` | `Authorization: Bearer org_demo` |
| **Participant** | `participant@hackforge.local` | `Password123!` | `Authorization: Bearer prt_demo` |
| **Judge Alpha** | `judge_a@hackforge.local` | `Password123!` | `Authorization: Bearer jdg_a_demo` |
| **Judge Beta** | `judge_b@hackforge.local` | `Password123!` | `Authorization: Bearer jdg_b_demo` |

### 3. Run Automated Tests (53 Tests)
```sh
docker compose exec app pytest -v
```
Executes all 53 unit, migration, integration, and end-to-end browser flow tests against PostgreSQL (`hackforge_test`) and SQLite test instances.

### 4. Run DOGFOOD Acceptance Checker
```sh
python3 run.py .dogfood.toml > acceptance-report.txt
```
Generates verified report confirming:
```
T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS

claimed T1, verified T1
```

---

## Key Routes & UI

- **Public Gallery**: `GET /projects` (Search, filter by track and event, view submitted projects).
- **Project Detail**: `GET /projects/{project_id}` (Public details without leaking judging scores).
- **Protected Media**: `GET /projects/{project_id}/media/{filename}` (Access-controlled draft media delivery).
- **Authentication**: `GET/POST /login`, `GET/POST /register`, `POST /logout`.
- **Events**: `GET /events`, `GET/POST /events/new` (Organizer creation), `GET/POST /events/{event_id}/manage` (Organizer dashboard).
- **Teams**: `GET/POST /events/{event_id}/teams/new`, `GET /teams/{team_id}`, `POST /teams/{team_id}/invite`, `POST /teams/{team_id}/invitations/{invitation_id}/revoke`, `GET/POST /invitations/{token}/accept`.
- **Project Submission**: `GET/POST /events/{event_id}/submit` (Drafting, final submission, thumbnail uploads with Pillow validation and concurrency control).
- **Observability Probes**: `GET /health/live`, `GET /health/ready`, `GET /health`.
