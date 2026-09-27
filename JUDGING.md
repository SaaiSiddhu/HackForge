# Judging Engine & Evaluation System

## Current Status: Phase 3 Implemented & Hardened (T1 Verified; T2 In Progress)

HackForge Phase 3 implements judge invitations, weighted rubrics, batch assignment, private scoring, OCC version locking, dual-role access control, and organizer progress tracking.

> **Phase Boundary Note**: In accordance with the DOGFOOD 2026 specification, T2 remains explicitly unclaimed in `.dogfood.toml` (`claimed = ["T1"]`). Cross-judge score normalization (Z-score calibration, zero-variance handling, sparse connectivity components) and comprehensive organizer CSV exports belong to the upcoming Phase 4 milestone.
> Voting, certificates, webhooks, and pairwise judging are out of scope.

---

## Implemented Architecture & Security Guarantees

### 1. Judge Identity & Explicit Account Association (No Automatic Claiming)
- **Strict Read-Only Lookup**:
  - `get_account_judge_for_event(db, event_id, account_id)` is strictly read-only (`SELECT ... WHERE event_id == :event_id AND account_id == :account_id`).
  - It NEVER queries by email address, never mutates `judge.account_id`, and never issues database commits.
- **Explicit Authorized Linking**:
  - Unlinked fixture or imported judges (`account_id IS NULL`) can only be linked to an account through an explicit, authorized invitation acceptance workflow.
  - When accepting an invitation (`accept_judge_invitation`), the recipient's authenticated account email must match the invitation email.
  - If a judge record for that email is already linked to another account, the system raises a conflict (`ValueError: Conflict: Judge record ... is already linked to another account`) and refuses the operation.

### 2. Rubric Lifecycle & Version Binding Policy
- **Immutability of Bound Rubrics**:
  - When a review is created, it is explicitly bound to the event's active rubric version at that moment (`review.rubric_id`).
  - When an organizer updates the rubric, if existing reviews exist under the current rubric, a new rubric version is created (`version = current_version + 1`).
  - When a judge edits an existing review, the review continues to validate against and calculate weighted scores using its **original bound rubric version**, preserving historical evaluation integrity.
  - New reviews for other projects always bind to the latest **active rubric version**.
- **Stale Form Rubric Rejection**:
  - The evaluation form and JSON API require `rubric_id`.
  - If a submitted `rubric_id` does not match the review's bound rubric (for edits) or the event's active rubric (for initial reviews), the request is rejected with `HTTP 400 Bad Request` (`ValueError: Rubric conflict ... Please reload`).

### 3. Review Concurrency & Atomicity Guarantees
- **Row-Level Locking & Fresh State**:
  - Existing reviews are fetched with `select(Review).where(...).with_for_update().execution_options(populate_existing=True)`.
  - A mandatory `session.refresh(locked_review)` guarantees that state loaded into the session identity map is refreshed with latest database state after waiting on a lock.
- **Mandatory `expected_version` on Edits**:
  - Modifying an existing review requires `expected_version`. If omitted or mismatched, the request raises `ConcurrencyConflictError`.
  - Initial review creation requires `expected_version=None` or `expected_version=0`.
- **Serialized Simultaneous Creation**:
  - To prevent simultaneous initial review creation for the same `(judge_id, project_id)`, `save_judge_evaluation` acquires a parent row lock on the `JudgeAssignment` row (`SELECT ... FROM judge_assignments WHERE ... FOR UPDATE`).
  - In addition, PostgreSQL unique constraint violations on `(judge_id, project_id)` are caught to rollback and raise clean `ConcurrencyConflictError`.
- **Isolated Conflict Audit Logging**:
  - When an OCC conflict occurs, `_record_conflict_safely` commits the `review.conflict` audit event in a completely separate, short-lived database transaction (`Session(db.get_bind())`).
  - The caller session is rolled back (`db.rollback()`), guaranteeing zero dirty caller mutations are committed.

### 4. Centralized Judging Read Authorization & Track Policy
- **Strict Track Eligibility**:
  - An empty track list `[]` strictly denotes **NO tracks permitted**.
  - An explicit wildcard marker `"*"` (e.g. `["*"]`) is required for **ALL tracks permitted**.
  - Consistent helper `is_judge_permitted_for_track(judge, track_id)` governs batch assignment, manual assignment, judge dashboard project listing, and evaluation submission.
- **Backend-Enforced Judge Isolation**:
  - `GET /events/{event_id}/judges/{judge_id}/scores` allows judges to inspect only their own scores. Accessing peer judge scores returns **HTTP 403 Forbidden**.
  - Participants attempting to access judge endpoints receive **HTTP 403 Forbidden**.
- **Dual-Role Organizer/Judge Access Restriction**:
  - Organizers who are registered as active judges in the event are strictly prohibited from inspecting live judging progress, scoring aggregates, or peer reviews to eliminate scoring bias.
  - Endpoint `GET /events/{event_id}/manage/progress` returns **HTTP 403 Forbidden** for dual-role users.
  - In the organizer management console (`GET /events/{event_id}/manage`), live assignment counts and judging progress cards are replaced with a clear warning: *"Dual-Role Notice: You are registered as an active judge for this event. Live judging progress and aggregates are hidden to prevent scoring bias."*
  - Dual-role users retain full access to their own judge dashboard (`/events/{event_id}/judge`) to evaluate their assigned submissions.

### 5. Assignment-Based Progress Calculation
- **Join Semantics**:
  - Progress metrics (`total_assignments`, `finalized_reviews`, `draft_reviews`, `unstarted_assignments`) are computed directly by outer-joining current `JudgeAssignment` rows to `Review` records.
  - An assignment with no review or a review in `unstarted` status counts as `unstarted_assignments`.
- **Historical Review Independence**:
  - If a project assignment is removed after a review was created, that review becomes historical unassigned review data.
  - Historical reviews do NOT reduce the unstarted count of any other active assignments.
  - The progress payload explicitly reports `total_historical_reviews` and `historical_unassigned_reviews`.

---

## Testing & Verification Status

- **Automated Pytest Suite**: 85 passing tests (0 failures), including:
  - 17 unit/integration tests in `tests/test_judging.py` covering invitation security, rubric versioning, track revocation, progress counts, and dual-role restrictions.
  - 8 real PostgreSQL integration tests in `tests/test_postgres_integration.py` covering concurrent two-session review creation, concurrent two-session edits from same version, and session cache invalidation under row locking.
- **Official Acceptance Checker**:
  - `python3 run.py .dogfood.toml` $\to$ **Exit code 0 (`claimed T1, verified T1`)**.
  - T2 judge isolation checks pass (`judge sees own scores: PASS`, `judge cannot see peer scores: PASS`, `participant blocked: PASS`).
- **Browser Testing Status**: **NOT VERIFIED**.
  - Headless browser automation tooling (e.g. Playwright/Puppeteer) is not installed in this execution environment.
  - End-to-end functionality is fully validated via Starlette/FastAPI HTTP client tests and transactional PostgreSQL tests.

---

## Upcoming Milestone: Phase 4 (Score Normalization & CSV Exports)
- Cross-judge Z-score calibration.
- Zero-variance handling ($\sigma_j = 0$).
- Connected-components graph analysis for sparse evaluation networks.
- Full organizer audit CSV export (`GET /events/{event_id}/export/judging.csv`).
- Official Dogfood T2 claim (`claimed = ["T1", "T2"]`).
