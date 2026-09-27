# Judging Engine & Evaluation System

## Current Status: Phase 3 Implemented (T1 Verified; T2 In Progress)

HackForge Phase 3 implements the complete judge invitation lifecycle, weighted rubrics, batch assignment, private scoring, OCC version locking, and organizer progress tracking.

> **Phase Boundary Note**: In accordance with the DOGFOOD 2026 specification, T2 remains explicitly unclaimed in `.dogfood.toml` (`claimed = ["T1"]`). Cross-judge score normalization (Z-score calibration, sparse connectivity components) and comprehensive organizer CSV exports belong to the upcoming Phase 4 milestone.
> Voting, certificates, webhooks, and pairwise judging are out of scope.

---

## Implemented Architecture (Phase 3)

### 1. Judge Invitation Lifecycle
- **Organizer Invitation Flow**:
  - Organizers create invitations with target email and optional track restrictions (`POST /events/{event_id}/manage/judges/invite`).
  - Generates a high-entropy 256-bit token (`secrets.token_urlsafe(32)`).
  - Only the cryptographic SHA-256 hash (`hashlib.sha256(token.encode()).hexdigest()`) is stored in `judge_invitations.token_hash`.
  - The raw one-time link (`/events/{event_id}/judge-invite/{token}`) is presented to the organizer once in an alert banner.
- **Invitation Acceptance**:
  - Visiting `GET /events/{event_id}/judge-invite/{token}` renders the invitation details (event name, track permissions, expiration date) without changing state (strictly idempotent GET).
  - Submitting `POST /events/{event_id}/judge-invite/{token}/accept` validates:
    - Token exists and matches SHA-256 hash.
    - Invitation status is `"pending"`.
    - Expiration date (`expires_at`, default 7 days) has not passed.
  - Registers the authenticated account as a `Judge` in `judges` and assigns the `"judge"` role in `event_roles`.
  - Grants track permissions in `judge_tracks` based on the invitation configuration.
  - Atomically marks the invitation `"accepted"` with `accepted_at` and `accepted_by_account_id`.
- **Revocation & Expiration**:
  - Organizers can revoke pending invitations (`POST /events/{event_id}/manage/judges/invitations/{inv_id}/revoke`).
  - Expired and revoked tokens reject acceptance attempts with descriptive error messages.

### 2. Weighted Scoring Rubrics
- **Configuration & Validation**:
  - Criteria define `key`, `name`, `weight` (> 0), `min_score`, `max_score` (`min_score < max_score`), and `sort_order`.
  - Rubric updates check whether existing reviews have already evaluated projects using the rubric.
  - **Version Freezing / Snapshotting**: If reviews exist for the current rubric, editing the rubric does not mutate existing completed evaluations. Instead, a new rubric version is created (`version = previous_version + 1`), preserving the historical rubric snapshot for older reviews while making the new version active for future evaluations.
- **Weighted Scoring Formula**:
  - The raw normalized composite score for a submission is calculated on a 0–100 scale:
    $$S_{\text{raw}} = \frac{\sum_{i=1}^n w_i \cdot s_i}{\sum_{i=1}^n w_i} \times 100$$
    where:
    - $w_i > 0$ is the criterion weight.
    - $s_i \in [0, 1]$ is the normalized criterion score:
      $$s_i = \frac{\text{raw\_score}_i - \min_i}{\max_i - \min_i}$$
    - If all criteria have $\min_i = \max_i$, $s_i$ defaults to 1.0.

### 3. Batch Judge Assignment Engine
- **Fair Load Balancing**:
  - Targets $k$ reviews per submitted project (`POST /events/{event_id}/manage/assignments/batch`).
  - Excludes draft projects (`status='draft'`) — only submitted projects receive assignments.
  - **Track Restrictions**: Judges with explicit track assignments are only allocated projects within their permitted tracks. Judges with no track restrictions are eligible across all tracks.
  - **Conflict of Interest Exclusion**: If a judge is an active team member of a project's team, the assignment engine strictly excludes that judge from evaluating their own team's submission.
  - Prioritizes judges with the lowest current workload and projects with the fewest assignments, using deterministic ID tie-breaking.
  - Accurately reports under-covered projects when the pool of eligible judges is insufficient.

### 4. Judge Scoring & Optimistic Concurrency Control (OCC)
- **Draft vs Finalized Reviews**:
  - Draft evaluations (`is_final=False`) allow judges to save partial criteria scores and draft comments without validation rejections.
  - Finalized evaluations (`is_final=True`) require all rubric criteria to be scored within numeric bounds.
- **Atomic Concurrency Control**:
  - Reviews acquire a row-level database lock (`SELECT ... FOR UPDATE`).
  - Reviews enforce optimistic version checking (`version_id_col` / `version`).
  - If a judge attempts to update a review with a stale `expected_version`, the service raises `ConcurrencyConflictError` and records an atomic conflict event in `event_audit_logs` (`action="review.conflict"`).

### 5. Backend-Enforced Judge Isolation
- **Strict Query Isolation**:
  - Endpoint `GET /events/{event_id}/judges/{judge_id}/scores` is protected on the server:
    - A judge can only access their own scores (`current_account.judge.id == judge_id`).
    - Attempting to inspect peer scores returns **HTTP 403 Forbidden**.
    - Participants attempting to access judge endpoints receive **HTTP 403 Forbidden**.
    - Organizers who are also active judges in the event are blocked from accessing peer scores to prevent judging bias.
- Evaluation routes (`GET/POST /events/{event_id}/judge/projects/{project_id}/review`) require:
  - Account must be an assigned judge for the specific project.
  - Project must belong to the judge's permitted tracks.
  - Violations return **HTTP 403 Forbidden**.

### 6. Organizer Progress Dashboard
- Real-time judging progress metrics displayed in the organizer event management console (`/events/{event_id}/manage`):
  - Total Judge Assignments
  - Finalized Evaluations
  - Draft Evaluations
  - Unstarted Assignments
  - Under-covered Projects count
  - Judges with Outstanding Workload count
- Interactive batch assignment trigger and judge invitation management table.

---

## Upcoming Milestone: Phase 4 (Score Normalization & CSV Exports)
- Cross-judge Z-score calibration.
- Zero-variance handling ($\sigma_j = 0$).
- Connected-components graph analysis for sparse evaluation networks.
- Full organizer audit CSV export (`GET /events/{event_id}/export/judging.csv`).
- Official Dogfood T2 claim.
