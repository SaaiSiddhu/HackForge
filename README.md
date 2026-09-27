# HackForge

**Build. Submit. Judge.** A self-hosted hackathon submission and judging platform for DOGFOOD 2026.

## Current Milestone: Phase 1 (Foundation)

Phase 1 establishes a tested, reproducible, and self-contained application foundation:

- **Framework**: FastAPI (Python 3.12) with Jinja2 server-side rendering.
- **Database**: PostgreSQL 17 with SQLAlchemy 2 (synchronous, transaction-safe) and Alembic migrations.
- **Fixtures**: Robust, validated, and idempotent importer for the official DOGFOOD 2026 dataset (`fixtures.json`).
- **Public Portal**: Responsive, accessible project gallery with real-time title search (wildcard-safe), track filtering, and deterministic sorting.
- **Project Detail**: Server-side rendered project page with validated repository links (`safe_repo_url`).
- **Privacy Enforcement**: Complete backend isolation of private judging data (reviewer emails, comments, and scores are never exposed).
- **Observability & Probes**: Standardized liveness (`/health/live`) and database readiness (`/health/ready`, `/health`) probes.
- **Assets**: 100% locally served static styles and semantic HTML. Zero external CDN, cloud service, or third-party API dependencies.

> **Honest Tier Status**: **No tiers are claimed yet (`claimed = []` in `.dogfood.toml`).**  
> Authentication, editable submissions, deadline enforcement, judge isolation endpoints, and scoring workflows belong to subsequent phases. Passing acceptance checks are verified only against implemented foundation routes.

---

## Quickstart

### Prerequisites
- Docker Engine 24+ and Docker Compose v2.

### 1. Start the Platform
```sh
docker compose up -d
```
The portal starts, waits for PostgreSQL readiness, runs database migrations, seeds fixtures, and binds to:
[http://localhost:8080](http://localhost:8080)

### 2. Check Health
```sh
curl http://localhost:8080/health/ready
```
Returns `{"status": "ok", "service": "hackforge", "database": "connected"}`.

### 3. Run Automated Tests
```sh
docker compose exec app python -m pytest -v
```
Executes all 33 automated tests covering settings validation, fixture imports, route behavior, privacy guarantees, migrations, and PostgreSQL integration.

### 4. Run Official Checker
```sh
python3 run.py .dogfood.toml
```

---

## Operational Characteristics

- **Zero Remote Dependencies**: Image build and initial dependency preparation require internet access. Once prepared, container startup and portal operation run entirely offline without internet connectivity.
- **Data Persistence**: Database files reside in the named Docker volume `database`. Restarts (`docker compose restart`) preserve local edits and existing records without data duplication.
- **Local Host Binding**: The application binds to `127.0.0.1:8080`. PostgreSQL is not published to host ports.
- **Non-Root Execution**: The application runs under an unprivileged `portal` user inside the container.
- **Safe Demo Data Reset**: Re-running fixture seeding preserves local edits. To deliberately reset demo data to pristine fixtures:
  ```sh
  docker compose exec app python -m app.seed --reset
  ```

---

## Official Dataset Summary

Downloaded from https://dogfoodhack.com/spec/ on September 27, 2026:
- **1 Event**: `evt_01` (Sample Hack 2026; submission deadline closed: `2026-03-01T18:00:00Z`).
- **8 Tracks**: Developer tools, Data & analytics, Accessibility, Security, Climate, Health, Education, Open hardware.
- **40 Teams**: `tm_01` through `tm_40`.
- **30 Judges**: `jdg_01` through `jdg_30`.
- **41 Project Submissions**: Including deliberate duplicate title `Dry Harbour` (`prj_07` and `prj_41`), both preserved.
- **126 Reviews**: Covers missing review distributions (2 to 5 reviews per project) and constant scoring patterns (`jdg_01`, `jdg_07`).

---

## Milestone Roadmap

1. **Phase 1 (Complete)**: Application foundation, migrations, fixture import, public gallery, security headers, PostgreSQL integration.
2. **Phase 2 (Next)**: Local password hashing, session tokens, event-scoped roles (participant, judge, organizer, admin), team invitations, draft/final submission writes, and server-side submission window enforcement.
3. **Phase 3**: Judge assignments, rubric weighting, review forms, backend-enforced judge isolation, and organizer progress tracking.
4. **Phase 4**: Statistical cross-judge normalization proof, CSV exports, audit history, and official acceptance report generation.
5. **Phase 5**: Offline runtime verification, final documentation, and video demonstration.
