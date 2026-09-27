# Judging Engine & Evaluation System

## Current Status: Phase 4 Complete (T1 and T2 Verified)

HackForge implements complete end-to-end judging, weighted rubrics, deterministic batch assignments, private evaluations, OCC row-level concurrency locking, dual-role access control, organizer progress tracking, defensible judge-wise Z-score normalization, reproducible snapshots, and RFC 4180 compliant CSV exports.

> **Official Acceptance Status**: `.dogfood.toml` claims `["T1", "T2"]`. Unmodified official checker `python3 run.py .dogfood.toml` verifies both T1 and T2 with exit code 0 (`claimed T1 T2, verified T1 T2`).
> Voting, certificates, webhooks, and pairwise judging remain strictly out of scope.

---

## 1. Score Normalization & Statistical Engine

### A. Mathematical Formulation
- **Raw Score Recalculation**:
  Raw scores are dynamically validated and recalculated from source criterion scores and active rubric weights:
  $$\text{Raw Score} = 100 \times \frac{\sum_{i=1}^C w_i \cdot \frac{s_i - \min_i}{\max_i - \min_i}}{\sum_{i=1}^C w_i}$$
  Unrounded float precision is maintained for all rankings and intermediate calculations. Rounding occurs only at presentation time.
- **Judge Cohort Statistics & Population Standard Deviation**:
  For each judge $j$ with evaluated cohort scores $S_j = [S_{j,1}, \dots, S_{j, N_j}]$:
  $$\mu_j = \frac{1}{N_j} \sum_{k=1}^{N_j} S_{j,k}$$
  $$\sigma_j = \sqrt{\frac{1}{N_j} \sum_{k=1}^{N_j} (S_{j,k} - \mu_j)^2}$$
  Note: $\sigma_j$ is the **population** standard deviation (matching `statistics.pstdev`).
- **Guardrails Against Sparse Samples & Zero Variance**:
  - Sample size threshold: $N_{\min} \ge 3$. Judges with $N_j < 3$ are excluded from normalization (`"insufficient_samples"`).
  - Variance threshold: $\epsilon = 1\text{e-}6$. Judges with $\sigma_j \le \epsilon$ are excluded from normalization (`"near_zero_variance"`).
  - Raw scores from unusable judges are preserved in raw aggregates and audit logs.
- **Z-Score Calibration & Project Normalized Index**:
  For usable judges:
  $$z_{j,k} = \frac{S_{j,k} - \mu_j}{\sigma_j}$$
  Project normalized index is the arithmetic mean of usable judge Z-scores:
  $$\text{Normalized Index}_k = \frac{1}{|Z_k|} \sum_{z \in Z_k} z$$
  - If $|Z_k| < \text{event.target\_reviews\_per\_project}$, project is marked **Provisional**.
  - If $|Z_k| = 0$, normalized index is left blank (`None` / empty string in CSV), **never fabricated as zero**.
- **Bipartite Evaluation Graph Connectivity**:
  A breadth-first search (BFS) partitions the project-judge evaluation graph into connected components. If $> 1$ components exist, cross-component normalization is uncalibrated, and a prominent warning is displayed.

---

## 2. Shared Review Eligibility Policy

Both results calculation and all CSV exports enforce an identical, shared eligibility policy:
1. **Draft vs Finalized**: Only reviews with `status == "finalized"` count toward project scores and rankings. Draft reviews are excluded (`"draft_review"`).
2. **Removed Assignments**: Finalized reviews whose assignment was subsequently removed remain eligible historical evaluations unless invalidated by conflict or track revocation.
3. **Conflicts of Interest**: A judge who is a member of the project's team (`judge.account_id` in team memberships) is immediately disqualified (`"conflict_of_interest"`).
4. **Project Lifecycle**: Projects with `status != "submitted"` (e.g. draft, withdrawn) are excluded (`"project_status_draft"`).
5. **Criterion Integrity**: Every criterion in the bound rubric must have a finite score within $[\min_i, \max_i]$. Any missing or out-of-bounds score invalidates the review (`"incomplete_or_invalid_criteria_scores"`).
6. **Rubric Binding**: Reviews reference specific rubric versions. If a bound rubric record is missing or inconsistent, calculation fails cleanly with an integrity error.
7. **Track Restrictions**: A judge with track restrictions who reviews a project outside their authorized tracks is excluded (`"track_mismatch"`).

---

## 3. Reproducible Organizer Results & Immutable Snapshots

- **Web Dashboard**: `/events/{event_id}/manage/results` provides:
  - Cohort selector (Whole Event or individual Tracks).
  - Prominent bipartite graph connectivity warnings.
  - Complete ranking table with normalized index, raw mean, coverage, and provisional indicators.
  - Judge calibration statistics ($N_j$, $\mu_j$, $\sigma_j$, usability status).
  - Excluded reviews table with explicit rejection reasons.
- **OCC Snapshot Freeze**: `POST /events/{event_id}/manage/results/snapshots` captures:
  - SHA-256 cryptographic hashes of all input review criteria and states.
  - Complete calculated results data.
  - Method and parameter tracking (`min_sample`, `epsilon`, `track_id`, `rubric_id`).
- **Live Staleness Detection**: Inspecting a stored snapshot automatically compares current review criteria hashes against snapshot input signatures, flagging added, modified, or deleted reviews.

---

## 4. Comprehensive Organizer CSV Exports

Available exclusively to authorized event organizers:
1. `GET /events/{event_id}/export/judging.csv`: Normalized and raw rankings by cohort or snapshot.
2. `GET /events/{event_id}/export/reviews.csv`: Detailed evaluation records with rubric versions and eligibility flags.
3. `GET /events/{event_id}/export/assignments.csv`: Judge assignments with completion and review status.
4. `GET /events/{event_id}/export/teams.csv`: Team rosters, captain IDs, and associated projects.
5. `GET /events/{event_id}/export/projects.csv`: Submission metadata, submission timestamps, and coverage counts.
6. `GET /events/{event_id}/export/judges-calibration.csv`: Sample counts, means, population standard deviations, and usability reasons.

**Export Guarantees**:
- **RFC 4180 Compliance**: Minimal quoting, CRLF (`\r\n`) line endings, clean multiline field handling.
- **UTF-8 Encoding**: Standard `text/csv; charset=utf-8` responses.
- **Formula Injection Defense**: Any cell value beginning with `=`, `+`, `-`, `@`, `\t`, or `\r` is escaped with a prepended single quote (`'`), while legitimate numeric negatives (e.g. `-1.2345`) remain pure numbers.
- **No Zero Fabrication**: Missing or unavailable metrics (e.g. unreviewed projects, unusable judges) are rendered as empty strings `""`, never fabricated zeros.

---

## 5. Strict Authorization Matrix

| User Role | Judge Dashboard (`/judge`) | Manage Progress (`/manage/progress`) | Results & Snapshots (`/manage/results`) | CSV Exports (`/export/*.csv`) |
| :--- | :---: | :---: | :---: | :---: |
| **Visitor (Unauthenticated)** | 401 | 401 | 401 | 401 |
| **Participant** | 403 | 403 | 403 | 403 |
| **Judge (Pure)** | 200 (Own) | 403 | 403 | 403 |
| **Dual-Role (Organizer + Active Judge)** | 200 (Own) | **403 (Hidden)** | **403 (Hidden)** | **403 (Hidden)** |
| **Organizer (Authorized)** | 403 / 200 | 200 | 200 | 200 |
| **Cross-Event Organizer** | 403 | 403 | 403 | 403 |

- **Snapshot Isolation**: Snapshot exports derive event ownership strictly from `snapshot.event_id`, preventing cross-event leakage even if a foreign `event_id` is supplied in the URL.

---

## 6. Verification & Test Summary

- **Pytest Suite**: **96 passing tests** across 15 test modules:
  - `tests/test_scoring.py`: 7 tests covering raw score math, eligibility edge cases, population std dev, near-zero variance guards, small sample guards, bipartite graph BFS, and snapshot staleness.
  - `tests/test_exports.py`: 4 tests covering formula injection, RFC 4180 quoting, blank values, dual-role export blocking, and snapshot event ownership isolation.
  - `tests/test_judging.py`: 17 tests covering invitations, rubric freezing, concurrency, and judge score isolation.
  - `tests/test_postgres_integration.py`: 8 tests covering concurrent PostgreSQL transactions and row locking.
- **Official Acceptance Checker**:
  - `python3 run.py .dogfood.toml` $\to$ **Exit code 0 (`claimed T1 T2, verified T1 T2`)**.
- **Browser Testing Status**: **NOT VERIFIED**.
  - Headless browser automation tooling is not installed in this execution environment. All UI routes, CSRF token handling, and rendering have been verified via Starlette TestClient integration tests.
