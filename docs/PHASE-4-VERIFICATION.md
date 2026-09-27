# HackForge Phase 4: Verification Report

**Date**: September 27, 2026  
**Canonical Workspace**: `/Users/saai_siddharth/Projects/Hackathon/HackForge`  
**Starting Commit**: `2e213af`  
**Dogfood Tier Claimed**: `["T1", "T2"]`  
**Dogfood Tier Verified**: `["T1", "T2"]`  
**Browser Testing Status**: **NOT VERIFIED** (Automated headless browser tooling is not installed in this environment)

---

## 1. Executive Summary

HackForge Phase 4 implements a defensible, mathematically rigorous judge evaluation and scoring engine, cross-judge Z-score normalization, reproducible OCC snapshotting, and RFC 4180 compliant CSV exports with formula injection defense.

All requirements set forth in the Phase 4 specification have been built, integrated with PostgreSQL and Alembic migrations, verified through automated unit and integration tests, and validated using the unmodified official DOGFOOD acceptance checker (`run.py`).

---

## 2. Core Capabilities Implemented

### A. Carryover Hardening & Historical Rubric Integrity
- **Historical Rubric Integrity**: In `app/judging_services.py`, reviews bound to a specific `rubric_id` strictly evaluate against that exact rubric. If the bound rubric is missing or inconsistent, a clear `ValueError` is raised. The system never silently falls back to the event's active rubric.
- **Coverage Configuration**: Replaced hardcoded `< 2` reviews threshold with `event.target_reviews_per_project` (persisted in database migration `0005`). Enforced across `get_organizer_judging_progress`, `run_batch_assignment`, results calculations, and CSV exports.

### B. Shared Review Eligibility Policy
Implemented a single, centralized evaluation policy (`evaluate_review_eligibility` in `app/scoring_services.py`):
1. **Status**: Only reviews with `status == "finalized"` are included; draft reviews are excluded (`"draft_review"`).
2. **Removed Assignments**: Completed finalized reviews remain eligible historical evaluations unless invalidated by conflict or track revocation.
3. **Conflicts of Interest**: Judges who belong to the evaluated project's team (`judge.account_id` in `TeamMembership`) are disqualified (`"conflict_of_interest"`).
4. **Project State**: Projects must have `status == "submitted"`. Draft or withdrawn submissions are excluded (`"project_status_draft"`).
5. **Criterion Validity**: Every criterion in the bound rubric must have a finite score within $[\min_i, \max_i]$. Any missing or invalid score excludes the review.
6. **Track Eligibility**: Judges with track restrictions who evaluate projects outside their assigned tracks are excluded (`"track_mismatch"`).

### C. Transparent Raw Scoring
- Dynamic recalculation from raw criterion rows:
  $$\text{Raw Total} = 100 \times \frac{\sum_{i=1}^C w_i \cdot \frac{s_i - \min_i}{\max_i - \min_i}}{\sum_{i=1}^C w_i}$$
- Full unrounded float precision retained for rankings and statistics; rounded only for presentation.
- Cohort separation by `(track_id, rubric_id)` and whole-event cohorts when evaluating single rubrics.

### D. Defensible Judge-Wise Z-Score Normalization
- Per-judge cohort statistics:
  - Sample size $N_j$
  - Sample mean $\mu_j$
  - Population standard deviation $\sigma_j = \sqrt{\frac{1}{N_j}\sum(S_{j,k}-\mu_j)^2}$ (matching `statistics.pstdev`)
- **Guardrails**:
  - Minimum sample size $N_{\min} \ge 3$: Judges with $N_j < 3$ are excluded (`"insufficient_samples"`).
  - Near-zero variance threshold $\epsilon = 1\text{e-}6$: Judges with $\sigma_j \le 1\text{e-}6$ are excluded (`"near_zero_variance"`).
- **Z-Scores & Normalized Index**:
  - Usable judge Z-score: $z_{j,k} = \frac{S_{j,k} - \mu_j}{\sigma_j}$
  - Project normalized index: mean of usable Z-scores.
  - Missing normalized metrics remain blank (`None` / empty string in CSV), **never fabricated as zero**.
  - Projects with usable evaluations below target coverage are flagged as **Provisional**.
- **Graph Connectivity**:
  - Bipartite BFS detects connected components among projects and usable judges.
  - Prominently warns on disconnected components where cross-component Z-scores are uncalibrated.

### E. Reproducible Organizer Results & Immutable Snapshots
- Organizer UI at `GET /events/{event_id}/manage/results`.
- Cohort switcher (Whole Event or per-track cohorts).
- Snapshot capture via `POST /events/{event_id}/manage/results/snapshots`, storing SHA-256 signatures of all input review criteria.
- Real-time staleness detection comparing stored signatures against live review states.

### F. Comprehensive Organizer CSV Exports
All 6 endpoints implemented with RFC 4180 compliance, minimal quoting, CRLF terminators, and formula injection defense (values starting with `=`, `+`, `-`, `@`, `\t`, `\r` are escaped with `'`, while valid numeric negatives remain bare numbers):
1. `GET /events/{event_id}/export/judging.csv`
2. `GET /events/{event_id}/export/reviews.csv`
3. `GET /events/{event_id}/export/assignments.csv`
4. `GET /events/{event_id}/export/teams.csv`
5. `GET /events/{event_id}/export/projects.csv`
6. `GET /events/{event_id}/export/judges-calibration.csv`

### G. Strict Judging Authorization
- Organizer-only access on all results and exports.
- **Dual-Role Restriction**: Organizers who are registered as active judges in the event are returned **HTTP 403 Forbidden** on `/manage/results`, `/manage/progress`, and all `/export/*.csv` endpoints to prevent scoring bias.
- **Cross-Event Isolation**: Organizers of other events cannot access or export judging data.
- **Snapshot Isolation**: Snapshot exports derive event ownership strictly from `snapshot.event_id`, preventing cross-event access attempts.

---

## 3. Official Dogfood Acceptance Checker Results

Command executed:
```bash
python3 run.py .dogfood.toml
```

Console Output (`acceptance-report.txt`):
```
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

## 4. Automated Test Suite Results

Test command executed inside the container:
```bash
docker compose exec app pytest -q
```

Results:
```
======================== 96 passed, 1 warning in 11.89s ========================
```

Test Breakdown:
- `tests/test_scoring.py`: 7 tests (Scoring math, eligibility policy, population std dev, near-zero variance guards, small sample guards, graph connectivity BFS, snapshot staleness)
- `tests/test_exports.py`: 4 tests (Formula injection defense, RFC 4180 quoting, blank values, dual-role export blocking, snapshot event ownership isolation)
- `tests/test_judging.py`: 17 tests (Judge invitations, rubric versioning, track permissions, batch assignment, OCC concurrency locking, judge score isolation)
- `tests/test_postgres_integration.py`: 8 tests (Real PostgreSQL concurrent transactions, OCC conflict handling, row locking, cache invalidation)
- `tests/test_migrations.py`: 3 tests (Alembic clean cycle SQLite, PostgreSQL idempotency, isolation guards)
- `tests/test_auth.py`: 6 tests (Password hashing, session lifecycle, CSRF tokens)
- `tests/test_submissions.py`: 7 tests (Project submission lifecycle, questions, gallery)
- `tests/test_teams.py`: 4 tests (Team capacity, invitations, ownership)
- `tests/test_media_policy.py`: 9 tests (Upload security, media access control)
- `tests/test_routes.py`: 12 tests (Public vs private endpoints)
- `tests/test_organizer_workflow.py`: 1 test (End-to-end organizer event management)
- `tests/test_seed.py`: 6 tests (Fixture importing and idempotency)
- `tests/test_foundation.py`: 2 tests (Foundation configs)
- `tests/test_config.py`: 5 tests (Environment validation)
- `tests/test_health.py`: 4 tests (Healthcheck probes)
- `tests/test_browser_flow.py`: 1 test (HTTP client simulation)

---

## 5. Verification Limitations & Explicit Disclosures

- **Browser Verification**: **NOT VERIFIED**. Automated browser tooling (such as Playwright, Puppeteer, or Selenium) is not installed in this execution environment. Verification was conducted using Starlette `TestClient` HTTP requests and live HTTP requests from `run.py`.
- **Database Volumes**: Preserved all original PostgreSQL fixture records (41 projects, 41 teams, 30 judges, 126 reviews, 378 criterion scores).
