# HackForge

**Build. Submit. Judge.** A self-hosted, offline-capable hackathon submission, judging, and statistical calibration platform built for **DOGFOOD 2026**.

- **Demo Video**: `[TODO: Insert 5-Minute Demo Video URL Here Before Final Submission]`
- **License**: [MIT](./LICENSE)
- **Technical Documentation**:
  - [System Architecture (`ARCHITECTURE.md`)](./ARCHITECTURE.md)
  - [Relational Data Model (`DATA-MODEL.md`)](./DATA-MODEL.md)
  - [Judging Engine & Normalization (`JUDGING.md`)](./JUDGING.md)
  - [Statistical Normalization & Cohort Analysis (`docs/NORMALIZATION.md`)](./docs/NORMALIZATION.md)
  - [Phase 5 Release Verification Report (`docs/PHASE-5-RELEASE-VERIFICATION.md`)](./docs/PHASE-5-RELEASE-VERIFICATION.md)
  - [5-Minute Demo Script (`docs/DEMO-SCRIPT.md`)](./docs/DEMO-SCRIPT.md)
  - [Submission & Handoff Checklist (`docs/SUBMISSION-CHECKLIST.md`)](./docs/SUBMISSION-CHECKLIST.md)
  - [Preserved Release & Rehearsal Evidence (`docs/release-evidence/README.md`)](./docs/release-evidence/README.md)

---

## 1. What HackForge Does

HackForge provides an end-to-end, self-hosted hackathon portal supporting four distinct user roles (**Visitor**, **Participant**, **Judge**, and **Organizer**):

- **Public Discovery & Gallery (`T1`)**: Searchable, track-filterable project gallery (`/projects`) and event directory (`/events`). Unsubmitted drafts and private judging records are strictly isolated from public views and API responses.
- **Participant & Team Lifecycle (`T1`)**: Argon2id account registration/login, revocable SHA-256 hashed sessions,CSRF-protected forms, single-team-per-event database enforcement, token-based team invitations with PostgreSQL `SELECT ... FOR UPDATE` capacity locking, draft saving, Pillow-validated image uploads (`PNG`, `JPEG`, `GIF`, `WebP`, max 5 MB) served via access-controlled `/projects/{id}/media/{filename}`, optimistic concurrency control (`expected_version`), and server-side submission deadline enforcement.
- **Organizer Event & Rubric Management (`T1` / `T2`)**: Event creation and configuration, track and prize management, persistent custom submission questions, weighted multi-criterion rubrics with automatic version snapshotting when completed reviews exist, one-time cryptographic judge invitations, and conflict-aware deterministic batch judge assignment.
- **Private Judging & Concurrency Safety (`T2`)**: Dedicated judge queue (`/events/{id}/judge`), draft and finalized rubric scoring, strict backend isolation (`HTTP 403` on peer judge scores, participant attempts, and unassigned projects), and row-level locking with optimistic concurrency control. Dual-role users (organizers who are also active judges in the same event) are automatically blocked from viewing aggregate progress, results, or exports for that event.
- **Defensible Score Normalization, Snapshots & CSV Exports (`T2`)**: Per-track comparable cohort Z-score normalization (`method="zscore_v1"`), small-sample ($N_j < 3$) and near-zero-variance ($\sigma_j \le 10^{-6}$) guardrails, bipartite graph connectivity analysis (withholding unqualified global normalized ranks across disconnected components), `REPEATABLE READ` canonical v2 results snapshots with staleness detection, and 6 RFC 4180 CSV exports (`judging.csv`, `reviews.csv`, `assignments.csv`, `teams.csv`, `projects.csv`, `judges-calibration.csv`).

---

## 2. Claimed Tiers & Known Limitations

- **Claimed & Verified Tiers**: **`T1` and `T2`** (`claimed = ["T1", "T2"]` in [`.dogfood.toml`](./.dogfood.toml), verified by unmodified `python3 run.py .dogfood.toml`).
- **Out-of-Scope Tiers (`T3`, `T4`)**: Community voting, public comments, reactions, winner certificates, outbound webhooks, pairwise comparison judging, and multi-node cloud deployment are intentionally not implemented.
- **Statistical & Operational Limitations**:
  - **Small-Sample Z-Score Calibration**: Within-judge Z-score normalization adjusts for differences in judge mean ($\mu_j$) and spread ($\sigma_j$), but when per-judge sample sizes are small ($N_j \in [3, 6]$) and assignments are non-random, observed judge statistics reflect both judge strictness and the specific projects assigned. Normalized indices should be interpreted alongside raw means and coverage flags (`Provisional` vs `Verified`).
  - **Disconnected Evaluation Components**: In events where judges only evaluate subsets of tracks (such as diagnostic cross-track pooling `all:rb_evt_01_v1` in `evt_01`), the evaluation network forms disconnected components with zero shared usable judges. HackForge detects this via BFS and withholds global normalized ranks (`normalized_rank = None`), reporting only component-local ranks (`Component 1 #1..#20`, `Component 2 #1..#21`).

---

## 3. Prerequisites & Quickstart

### Prerequisites
- **Docker Engine** 24+ and **Docker Compose** v2 (`docker compose`).
- **Python 3.10+** on the host (only if running the external `python3 run.py .dogfood.toml` acceptance checker from the host).

### Start HackForge (`docker compose up`)
From a clean clone of the repository:

```sh
docker compose up --build -d
```

Or in the foreground:

```sh
docker compose up
```

What happens automatically on startup:
1. Starts `postgres:17-alpine` with persistent named volume `database` and waits for `pg_isready`.
2. Builds and runs the FastAPI application container as non-root user `portal` (`uid=1000`) with persistent named volume `uploads` mounted at `/portal/uploads`.
3. Runs `alembic upgrade head` (migrations `0001` through `0005`).
4. Runs `python -m app.seed` under a PostgreSQL transaction-level advisory lock (`0x4841434B`), importing the official `fixtures.json` dataset (`evt_01`: "Sample Hack 2026" with 8 tracks, 40 teams, 30 judges, 41 projects including deliberate duplicate title `prj_07`/`prj_41`, and 126 reviews) plus the open demonstration event (`evt_open_demo`: "HackForge Spring Demo Hackathon 2026") and local evaluation accounts.
5. Serves the application at **[http://localhost:8080](http://localhost:8080)** (health probes: `/healthz`, `/health`, `/health/live`, `/health/ready`).

> **Port Override**: If port `8080` is occupied on your machine, run `APP_PORT=18080 docker compose up -d` to bind `http://127.0.0.1:18080`.
> **Optional Development Override**: `docker-compose.yml` is self-contained and does not bind-mount the host working tree or expose PostgreSQL on the host. For live-reload local development and host `5432` access, use `docker compose -f docker-compose.yml -f docker-compose.dev.yml up`.

---

## 4. First-Build Online Preparation vs. Offline Runtime

- **First Build (Online)**: Building the Docker image for the first time (`docker compose build` or first `docker compose up --build`) requires internet access to pull `python:3.12-slim` and `postgres:17-alpine` and install pinned Python wheels from [`requirements.lock`](./requirements.lock).
- **Offline Runtime (Air-Gapped / No Internet)**: Once images are built locally, HackForge operates **100% offline**:
  - All HTML templates, CSS (`app/static/style.css`), system font stacks, and uploaded media are served locally by the container. Zero external CDNs, Google Fonts, external scripts, or third-party APIs are referenced.
  - Database communication occurs strictly over the internal Docker bridge network (`db:5432`).
  - See [`docs/PHASE-5-RELEASE-VERIFICATION.md`](./docs/PHASE-5-RELEASE-VERIFICATION.md) for evidence from running HackForge on an `internal: true` air-gapped Docker network with `pull_policy: never`.

---

## 5. Pre-Seeded Demo Credentials & Evaluation Headers

When `ENABLE_DEMO_PROVISIONING=true` (enabled in the default local evaluation `docker-compose.yml`, and **strictly refused** if `ENVIRONMENT=production`), `app.seed` provisions four local demo accounts and API Bearer tokens matching [`.dogfood.toml`](./.dogfood.toml):

| Role | Email | Password | API Auth Header (`.dogfood.toml`) | Assigned Events |
| :--- | :--- | :--- | :--- | :--- |
| **Organizer / Admin** | `organizer@hackforge.local` | `Password123!` | `Authorization: Bearer org_demo` | `evt_01` (Closed), `evt_open_demo` (Open) |
| **Participant** | `participant@hackforge.local` | `Password123!` | `Authorization: Bearer prt_demo` | `evt_01` (`tm_01`), `evt_open_demo` (`tm_open_demo`) |
| **Judge Alpha** | `judge_a@hackforge.local` | `Password123!` | `Authorization: Bearer jdg_a_demo` | `evt_01` (`jdg_01`), `evt_open_demo` (`jdg_open_a`) |
| **Judge Beta** | `judge_b@hackforge.local` | `Password123!` | `Authorization: Bearer jdg_b_demo` | `evt_01` (`jdg_02`), `evt_open_demo` (`jdg_open_b`) |

### Session Expiry & Refreshing Evaluation Tokens
- Demo Bearer sessions are stored as SHA-256 token hashes in the `sessions` table with a **365-day expiration window** from initial seeding.
- If an instance has been running past session expiry, or if you need to refresh expired demo sessions without losing data, run:
  ```sh
  docker compose exec app python -m app.seed
  ```
  `app.seed` is idempotent: it extends any expired demo session `expires_at` timestamps and ensures missing fixture/demo records exist while preserving all existing projects, reviews, judge `account_id` links, and local edits.

---

## 6. Running Automated Tests & the Official Acceptance Checker

### Run the Full Pytest Suite
```sh
docker compose exec app pytest -v
```
- Runs **100 automated tests** (`0 failed, 0 skipped`) across unit, migration, security/RBAC, media policy, normalization, snapshot provenance, CSV export, end-to-end HTTP flow, and multi-session PostgreSQL concurrency (`SELECT ... FOR UPDATE` and `REPEATABLE READ`) suites.
- Tests execute against an isolated `hackforge_test` PostgreSQL database and temporary SQLite/upload directories, never touching the live `hackforge` database or `/portal/uploads`.

### Run the Unmodified Official Acceptance Checker (`run.py`)
```sh
python3 run.py .dogfood.toml > acceptance-report.txt
cat acceptance-report.txt
```
Expected output (`exit code 0`):
```text
DOGFOOD 2026 acceptance report
portal: http://localhost:8080
claimed: T1 T2
fixtures: fixtures.json

T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS

claimed T1 T2, verified T1 T2
```

---

## 7. Data Persistence, Backups & Reset Cautions

- **Persistent Named Volumes**:
  - `database`: Stores PostgreSQL 17 data files (`/var/lib/postgresql/data`).
  - `uploads`: Stores validated project thumbnail images (`/portal/uploads`).
- **Safe Restarts**: Running `docker compose restart`, `docker compose stop`, or `docker compose down` (without `-v`) preserves all database records and uploaded images.
- **Destructive Reset Cautions**:
  - **Do not** run `docker compose down -v` on an active instance unless you intend to permanently delete the `database` and `uploads` volumes.
  - `python -m app.seed --reset` is blocked by default in PostgreSQL environments unless `ALLOW_DESTRUCTIVE_RESET=true` is explicitly passed in a non-production environment.
  - To back up your database before maintenance:
    ```sh
    docker compose exec db pg_dump -U hackforge -d hackforge > hackforge_backup.sql
    ```
