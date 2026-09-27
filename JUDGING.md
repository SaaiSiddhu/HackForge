# Judging Engine & Evaluation System

## Current Status: Phase 4 Results-Integrity Repair & Release Verification Complete (T1 and T2 Verified)

HackForge implements end-to-end judging, weighted rubrics, deterministic batch assignments, private evaluations, OCC row-level concurrency locking, dual-role access control, organizer progress tracking, defensible judge-wise Z-score normalization across comparable track/rubric cohorts, consistent-read canonical v2 manifest snapshots, comprehensive staleness detection, and RFC 4180 compliant CSV exports.

> **Official Acceptance Status**: `.dogfood.toml` claims `["T1", "T2"]`. Unmodified official checker `python3 run.py .dogfood.toml` verifies both T1 and T2 with exit code 0 (`claimed T1 T2, verified T1 T2`).
> Voting, certificates, webhooks, and pairwise judging remain strictly out of scope.

---

## 1. Score Normalization, Cohort Policy & Statistical Engine

### A. Comparable Cohorts vs. Diagnostic Cross-Track Pooling
- **Default Primary Cohorts `(event_id, track_id, rubric_id)`**:
  - Results calculation (`app/scoring_services.py:calculate_event_results`) defaults to comparable `(track_id, rubric_id)` cohorts (`cohorts[0..7]` in `evt_01`).
  - Within each track cohort of `evt_01`, all evaluated projects and usable judges form a single connected component (`is_connected = True`).
- **Explicit Diagnostic Cross-Track Pooling (`track_id="all"` / `all:<rubric_id>`)**:
  - Whole-event cross-track pooling (`is_diagnostic_pooling = True`) is available as an explicit diagnostic option (`?track_id=all` or `?cohort_id=all:rb_evt_01_v1`).
  - `track_id="all"` is explicitly handled in `calculate_results_from_manifest` so it includes all submitted projects and reviews across tracks rather than filtering for a literal track named `"all"`.
- **Disconnected Component Ranking Policy**:
  - A breadth-first search (`analyze_bipartite_connectivity`) partitions the project-judge evaluation network into connected components.
  - When `component_count <= 1` (`is_connected = True`), projects receive both `normalized_rank` and `component_normalized_rank`.
  - When `component_count > 1` (`is_connected = False`, such as the 2 disconnected components in `all:rb_evt_01_v1`), HackForge **withholds any unqualified global `normalized_rank` (`normalized_rank = None`)** and reports only component-local ranks (`component_normalized_rank`: `Component 1 #1..#20`, `Component 2 #1..#21`) across the UI, JSON API, snapshots, and `judging.csv`.

### B. Mathematical Formulation & Input Validation
- **Raw Score Recalculation**:
  $$\text{Raw Score} = 100 \times \frac{\sum_{i=1}^C w_i \cdot \frac{s_i - \min_i}{\max_i - \min_i}}{\sum_{i=1}^C w_i}$$
  Full IEEE 754 float precision is maintained for all intermediate calculations and rankings; rounding occurs only at presentation/export time.
- **Strict Input Validation (No `1.0` Fallback)**:
  - Both `calculate_raw_review_score` (`app/scoring_services.py`) and `calculate_raw_score` (`app/judging_services.py`) reject zero or negative criterion score ranges ($\max_i \le \min_i$), negative or non-finite weights, non-positive total weight, unknown criterion keys, boolean values, `NaN`/`±inf` scores, and out-of-range scores.
  - `validate_normalization_parameters` enforces integer `min_sample >= 1`, finite `epsilon > 0.0`, and supported `method` (`"zscore_v1"`).
- **Judge Cohort Statistics & Guardrails**:
  - Uses population standard deviation $\sigma_j = \text{pstdev}(S_j)$.
  - Excludes judges with $N_j < \text{min\_sample}$ (`3`) or $\sigma_j \le \epsilon$ (`1e-6`) from Z-score normalization while preserving their eligible raw scores in `raw_mean`.
  - Projects with `usable_normalized_count < event.target_reviews_per_project` are marked `is_provisional = True`. Projects with `0` usable normalized evaluations have `normalized_index = None` (rendered as `""` in CSV, never fabricated as `0`).

---

## 2. Unified Review Eligibility & Revocation Policy

`app/scoring_services.py:evaluate_review_eligibility` reuses the centralized permission helper `app/judging_services.py:is_judge_permitted_for_track`:
1. **Track Permissions & Revocation**:
   - `["*"]` explicitly permits all tracks.
   - Explicit track IDs permit those tracks.
   - `[]` or `None` permits **no tracks**.
   - **Revocation Policy**: Revoking a judge's track permission invalidates historical reviews for unauthorized tracks across live results, diagnostics (`excluded_reviews`), and live CSV exports (`ineligibility_reason = "track_mismatch"`). Historical snapshots retain the eligibility state captured in their canonical v2 manifest at snapshot time.
2. **Draft vs Finalized**: Only `status == "finalized"` reviews are eligible (`draft_review` excluded).
3. **Project Lifecycle**: Only `status == "submitted"` projects are eligible (`project_status_<status>` excluded).
4. **Conflicts of Interest**: Any judge whose `account_id` belongs to the project's `TeamMembership` is excluded (`conflict_of_interest`).
5. **Historical Rubric Integrity & Criterion Completeness**: Missing rubrics (`missing_rubric`), invalid rubric weights/ranges, unknown criterion keys (`unknown_criterion_<key>`), non-finite scores (`non_finite_criterion_<key>`), missing scores (`missing_criterion_<key>`), or out-of-bounds scores (`out_of_bounds_criterion_<key>`) are explicitly rejected.

---

## 3. Consistent-Read Canonical v2 Snapshots & Staleness Detection

- **Consistent-Read Single-Pass Manifest (`capture_calculation_manifest`)**:
  - On PostgreSQL, `create_results_snapshot` starts a clean transaction with `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ` before reading any rows.
  - All calculation inputs (event `target_reviews_per_project`, tracks, bound rubrics/criteria/weights/ranges/versions, teams & conflict-relevant memberships, judges & track permissions, assignments, all event projects & statuses/tracks, and reviews & criterion scores) are captured in one read pass into a self-contained v2 manifest (`manifest_version = 2`).
  - `calculate_results_from_manifest(manifest)` computes `results_data` purely from this in-memory manifest with zero additional DB queries, guaranteeing consistency between stored provenance and output.
  - `reproduce_snapshot_results(snapshot)` deterministically reproduces `snapshot.results_data` from `snapshot.input_review_hashes["manifest"]` even after live database records change.
  - Pre-existing snapshots lacking a v2 manifest are preserved in the database and explicitly marked `is_legacy_unverifiable = True` (`provenance_status = "legacy_unverifiable"`).
- **Comprehensive Staleness Detection (`check_snapshot_staleness`)**:
  - Compares the stored canonical calculation state against live state across all calculation-affecting domains: normalization method/parameters, event `target_reviews_per_project`, rubric weights/ranges/versions, judge track permissions/account bindings, conflict-relevant team memberships, project status/track/team, and review additions/removals/status/rubric bindings/scores.
  - Non-calculation changes (e.g., project summary/repo URL, judge display name, or unrelated events) do not mark a snapshot stale.

---

## 4. Comprehensive Organizer CSV Exports & Authorization Matrix

Available exclusively to authorized non-judge event organizers (`check_results_authorization` and `_load_authorized_snapshot`):
1. `GET /events/{event_id}/export/judging.csv`: Includes `component_id` and `component_normalized_rank`; withholds global `normalized_rank` (`""`) when the cohort graph is disconnected. Supports `?cohort_id=...`, `?track_id=...`, and `?snapshot_id=...`.
2. `GET /events/{event_id}/export/reviews.csv`: Uses unified `evaluate_review_eligibility` and supports `?snapshot_id=...` to export historical review states from v2 snapshots.
3. `GET /events/{event_id}/export/assignments.csv`: Judge assignments with completion status.
4. `GET /events/{event_id}/export/teams.csv`: Team rosters and submitted project links.
5. `GET /events/{event_id}/export/projects.csv`: Project metadata and assignment/review counts.
6. `GET /events/{event_id}/export/judges-calibration.csv`: Cohort judge calibration statistics ($N_j, \mu_j, \sigma_j$, usability flags); supports `?snapshot_id=...`.

| User Role | Judge Dashboard (`/judge`) | Manage Progress (`/manage/progress`) | Results & Snapshots (`/manage/results`) | CSV Exports (`/export/*.csv`) |
| :--- | :---: | :---: | :---: | :---: |
| **Visitor (Unauthenticated)** | 401 | 401 | 401 | 401 |
| **Participant** | 403 | 403 | 403 | 403 |
| **Judge (Pure)** | 200 (Own) | 403 | 403 | 403 |
| **Dual-Role (Organizer + Active Judge)** | 200 (Own) | **403 (Hidden)** | **403 (Hidden)** | **403 (Hidden)** |
| **Organizer (Authorized)** | 403 / 200 | 200 | 200 | 200 |
| **Cross-Event Organizer** | 403 | 403 | 403 | 403 |

---

## 5. Verification & Test Summary

- **Pytest Suite**: **100 passing tests** (`0 failed, 0 skipped`) across 15 test modules, including isolated PostgreSQL concurrency (`SELECT ... FOR UPDATE`) and `REPEATABLE READ` snapshot isolation tests on `hackforge_test`.
- **Official Acceptance Checker**: `python3 run.py .dogfood.toml` $\to$ **Exit code 0 (`claimed T1 T2, verified T1 T2`)**.
- **Clean-Clone & Migration Upgrade Verification**: **PASS** — Verified from a clean `git checkout-index` export in an isolated Compose project (`hackforge-clean`) with fresh volumes, non-root `uid=1000(portal)` uploads, container restart persistence, repeated seed idempotence, and step-by-step migration upgrade `0001 -> 0002 -> 0003 -> 0004 -> 0005`.
- **Offline Runtime Verification**: **PASS** — Verified in an air-gapped Docker bridge network (`internal: true`, `pull_policy: never`) with outbound TCP/DNS blocked (`Network is unreachable`). All 7 capabilities (readiness, gallery/CSS, auth, draft/PNG upload, batch assignment/scoring, results/snapshots, and all 6 CSV exports) succeeded offline.
- **Real Browser Verification**: **PASS** — Verified via headless Google Chrome (`Chrome/154.0.8037.57`) over Chrome DevTools Protocol across all 14 user journeys, with `0` horizontal overflow issues at `1280x800` desktop and `375x812` mobile viewports, `0` unlabeled form controls, and verified `:focus-visible` keyboard navigation. See [`docs/PHASE-5-RELEASE-VERIFICATION.md`](./docs/PHASE-5-RELEASE-VERIFICATION.md).
