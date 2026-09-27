# Phase 4 Verification & Results-Integrity Repair Report

## 1. Executive Summary

All Phase 4 results-integrity repairs have been implemented and verified on top of commit `efbe5e3` without adding out-of-scope product features (no T3/T4 voting, certificates, webhooks, or pairwise judging) and while preserving all live PostgreSQL data, official fixture records (`evt_01`), historical reviews, and existing snapshots.

- **Test Suite Status**: **99 passed, 0 failed, 0 skipped** (`docker compose exec -T app pytest -v`).
- **Official Acceptance Harness**: `python3 run.py .dogfood.toml` passes all 7 checks (`claimed T1 T2, verified T1 T2`).
- **Database Integrity**:
  - `alembic current` is at `0005 (head)` with no modified historical migrations.
  - All 41 projects, 41 teams, 30 judges, 126 reviews, and 378 criterion score rows in `evt_01` are intact.
  - Both pre-existing snapshots (`snp_evt_01_1790513395_0391a7eb`, `snp_evt_01_1790513663_ac35a82c`) are preserved and explicitly reported as `legacy_unverifiable`, alongside a newly verified v2 canonical manifest snapshot (`snp_evt_01_1790516956_6962cb97`).

---

## 2. Defects Resolved

### 1. Unified Review Eligibility & Revocation Policy
- **Defect**: `app/scoring_services.py:evaluate_review_eligibility` previously checked `if judge.tracks and len(judge.tracks) > 0 and "*" not in judge.tracks`, treating `[]` or `None` as unrestricted track access and contradicting `app/judging_services.py:is_judge_permitted_for_track`.
- **Resolution**:
  - `evaluate_review_eligibility` now delegates directly to `is_judge_permitted_for_track(judge, proj_track_id)`: `["*"]` permits all tracks, explicit track IDs permit those tracks, and `[]` or `None` permits no tracks (`"track_mismatch"`).
  - Documented and enforced consistently across live results, diagnostics (`excluded_reviews`), and CSV exports (`reviews.csv`, `judging.csv`) that revoking a judge's track permission invalidates affected historical reviews in live calculations while historical snapshots retain their point-in-time manifest state.
  - Verified by `test_unified_track_eligibility_across_results_diagnostics_and_csv` in `tests/test_scoring.py`.

### 2. Strict Rejection of Invalid Scoring Inputs & Normalization Parameters
- **Defect**: `app/scoring_services.py:calculate_raw_review_score` and `app/judging_services.py:calculate_raw_score` substituted `1.0` when `max_score - min_score <= 0` and did not validate all rubric weights, unknown criterion keys, or normalization parameters.
- **Resolution**:
  - Removed the `denom <= 0: term = 1.0` fallback from both `calculate_raw_review_score` and `calculate_raw_score`.
  - Added strict validation rejecting non-finite or negative criterion weights, non-positive or non-finite total rubric weight, non-finite or non-positive score ranges (`max_score <= min_score`), unknown criterion keys (`unknown_criterion_<key>` / `ValueError`), boolean inputs, `NaN`/`±inf` scores, and out-of-bounds scores.
  - Added `validate_normalization_parameters(min_sample, epsilon, method)` enforcing integer `min_sample >= 1`, finite float `epsilon > 0.0`, and recognized `method` (`"zscore_v1"`), returning HTTP 400 on invalid query/body parameters in `app/routers/results.py`.
  - Verified by `test_calculate_raw_review_score_and_parameters_reject_invalid_inputs` in `tests/test_scoring.py`.

### 3. Consistent-Read Canonical v2 Manifest Snapshots & Reproducibility
- **Defect**: `create_results_snapshot` previously ran `calculate_event_results` and then separately ran `compute_event_review_signatures` under `READ COMMITTED`, allowing concurrent writes to cause tearing between `results_data` and `input_review_hashes`, and stored only review hashes rather than a full reproducible manifest.
- **Resolution**:
  - Implemented `capture_calculation_manifest` (`app/scoring_services.py`), which begins a clean `REPEATABLE READ` transaction on PostgreSQL before reading any rows and captures all calculation inputs (event `target_reviews_per_project`, tracks, rubrics/criteria/weights/ranges/versions, teams & memberships, judges & track permissions, assignments, all event projects & statuses/tracks, and reviews & criterion scores) in a single consistent pass.
  - Implemented `calculate_results_from_manifest(manifest)` as a pure deterministic function that computes `results_data` solely from the captured manifest without querying the database again, and `reproduce_snapshot_results(snapshot)` to recompute `results_data` purely from `snapshot.input_review_hashes["manifest"]`.
  - Preserved existing snapshots in PostgreSQL and marked snapshots without `manifest_version == 2` as `is_legacy_unverifiable = True` (`provenance_status = "legacy_unverifiable"`).
  - Verified on real PostgreSQL (`hackforge_test`) by `test_postgres_snapshot_consistent_read_and_reproduction_under_concurrent_review_change` in `tests/test_postgres_integration.py`.

### 4. Comprehensive Snapshot Staleness Detection
- **Defect**: `check_snapshot_staleness` previously compared only review status and criterion hashes.
- **Resolution**:
  - Updated `check_snapshot_staleness` to compare the canonical calculation state (`_extract_calculation_state`) across all calculation-affecting domains: normalization method and parameters, event `target_reviews_per_project`, rubric weights/ranges/versions, judge track permissions and account bindings, conflict-relevant team memberships, project status/track/team, and review additions/removals/status/rubric bindings/criterion scores.
  - Non-calculation fields (e.g., project summary/repo URL, judge display name, or changes in other events) are excluded from the calculation state so they do not falsely mark a snapshot stale.
  - Verified by `test_comprehensive_snapshot_staleness_and_reproducibility` in `tests/test_scoring.py`.

### 5. Comparable Cohort Defaults, `track_id="all"` Fix, and Disconnected Component Rankings
- **Defect**: Default results pooled reviews across all tracks into a disconnected whole-event ranking, and passing `track_id="all"` filtered for `Project.track_id == "all"`, returning 0 projects and 0 reviews.
- **Resolution**:
  - Fixed `track_id="all"` filtering in `calculate_results_from_manifest` so `track_id not in (None, "all")` filters by literal track ID while `track_id == "all"` selects the cross-track diagnostic cohort.
  - Ordered comparable `(track_id, rubric_id)` cohorts first (`trk_01:rb_evt_01_v1` through `trk_08:rb_evt_01_v1`) as the default cohorts, and placed `all:<rubric_id>` last as an explicit diagnostic option (`is_diagnostic_pooling = True`).
  - Added `component_normalized_rank` per connected component and withheld unqualified global `normalized_rank` (`normalized_rank = None`, blank `""` in `judging.csv`) whenever `graph_connectivity.is_connected` is `False` (`component_count > 1`).
  - Regenerated `docs/NORMALIZATION.md` from the corrected calculations with objective descriptions of rank differences.
  - Verified by `test_cohorts_track_selection_rubric_versions_and_disconnected_ranking` in `tests/test_scoring.py`.

### 6. Export & Snapshot Authorization Verification
- **Defect**: Needed full verification of stored `snapshot.event_id` ownership and historical snapshot export immutability across all roles and routes.
- **Resolution**:
  - Added `_load_authorized_snapshot` in `app/routers/results.py`, verifying `check_results_authorization` against both the request `event_id` and the stored `snapshot.event_id`.
  - Extended snapshot export support to `reviews.csv` (`?snapshot_id=...`) in addition to `judging.csv` and `judges-calibration.csv`.
  - Verified visitors (`401`), participants (`403`), pure judges (`403`), dual-role judge/organizers (`403`), cross-event organizers (`403`), and authorized organizers (`200`/`201`) across results view, snapshot creation, snapshot viewing, all 6 CSV export routes, and snapshot-based exports in `tests/test_exports.py`.

---

## 3. Release Verification Checklist

| # | Verification Item | Status | Evidence |
| :--- | :--- | :---: | :--- |
| 1 | PostgreSQL migrations and schema constraints | **PASS** | `alembic current` $\to$ `0005 (head)`; `tests/test_migrations.py` & `tests/test_postgres_integration.py` |
| 2 | Official fixture preservation | **PASS** | 41 projects, 41 teams, 30 judges, 126 reviews, 378 criterion scores preserved in `evt_01`; `tests/test_seed.py` |
| 3 | T1 public gallery, detail pages, search/filter, and closed-event enforcement | **PASS** | `tests/test_routes.py`, `tests/test_submissions.py`, and `python3 run.py .dogfood.toml` T1 checks |
| 4 | T2 invitations, assignment, rubric versioning, private scoring, progress, normalization, and exports | **PASS** | `tests/test_judging.py`, `tests/test_scoring.py`, `tests/test_exports.py`, and `python3 run.py .dogfood.toml` T2 checks |
| 5 | Concurrency control for projects and reviews | **PASS** | `tests/test_postgres_integration.py` (`test_postgres_concurrent_two_sessions_occ`, `test_postgres_concurrent_review_creation_two_sessions`, `test_postgres_concurrent_review_edits_from_same_version`, `test_postgres_review_session_loaded_before_commit`) |
| 6 | Snapshot consistency and staleness detection | **PASS** | `test_postgres_snapshot_consistent_read_and_reproduction_under_concurrent_review_change` & `test_comprehensive_snapshot_staleness_and_reproducibility` |
| 7 | Access-control matrix including dual-role restrictions | **PASS** | `test_full_authorization_matrix_results_snapshots_and_all_exports` (`tests/test_exports.py`) & `test_dual_role_organizer_judge_access_restrictions` (`tests/test_judging.py`) |
| 8 | Browser verification status | **NOT VERIFIED** | Verified via Starlette `TestClient` HTTP tests; no headless browser automation run |
| 9 | Offline/container verification status | **NOT VERIFIED** | Verified inside local Docker Compose containers (`app` + `db`), but strict network-disconnected build/run was not executed |
