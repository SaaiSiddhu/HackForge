# HackForge — 5-Minute Hackathon Demo Script (`T1` & `T2`)

This script is structured for a single-take, 5-minute screen recording demonstrating HackForge's self-hosted packaging, participant submission lifecycle, private judge evaluation, defensible Z-score normalization, canonical v2 snapshots, and RFC 4180 CSV exports.

---

## Pre-Recording Rehearsal Checklist

1. **Terminal Ready**:
   - Open a terminal in `/Users/saai_siddharth/Projects/Hackathon/HackForge` with legible font size (`16pt+`).
   - Confirm containers are healthy: `docker compose ps` (`hackforge-app-1` on `127.0.0.1:8080`, `hackforge-db-1` healthy).
2. **Browser Windows Prepared**:
   - **Window 1 (Main / Participant -> Organizer)**: Normal Chrome window at `http://localhost:8080/projects`.
   - **Window 2 (Incognito / Judge Alpha)**: Incognito Chrome window at `http://localhost:8080/login`.
3. **Sample Image Ready**:
   - Have a small valid `.png` or `.jpg` image (`<= 5 MB`) on your Desktop or Downloads folder ready to select during the submission step.

---

## Timed Walkthrough & Narration Script

### `0:00–0:30` — Clean Startup, Health Check & Public Gallery (`T1`)

- **Action (Terminal)**:
  ```sh
  docker compose up -d
  curl -s http://localhost:8080/healthz
  ```
- **Action (Browser Window 1)**:
  - Open `http://localhost:8080/projects`.
  - Filter by Track **`Developer tools`** (`trk_01`) and search **`Slow Trail`**, then click **Clear** and open **`Glass Signal`** (`/projects/prj_01`).
  - Click **Events** (`/events`) in the top navigation bar.
- **Expected Visible Result**:
  - Terminal shows `{"status":"ok","service":"hackforge","database":"connected"}`.
  - Public gallery displays the 41 seeded submissions from `fixtures.json` (including deliberate duplicate title `"Dry Harbour"` under distinct IDs `prj_07` and `prj_41`), with zero private judge scores or reviewer identities exposed.
  - `/events` shows **Sample Hack 2026** (`Closed`) and **HackForge Spring Demo Hackathon 2026** (`Open for Submissions`).
- **Suggested Narration**:
  > *"HackForge is a self-hosted, offline-capable hackathon submission and judging platform built with FastAPI and PostgreSQL 17. A single `docker compose up` runs Alembic migrations, acquires a PostgreSQL advisory lock, and seeds the official 41-project fixture dataset. Unauthenticated visitors can search and filter the public gallery by track without any private judging data leaking."*

---

### `0:30–1:30` — Participant Login, Team Management, Draft Save, Image Upload & Final Submission (`T1`)

- **Action (Browser Window 1)**:
  1. Click **Log In** (`/login`) and sign in as Demo Participant:
     - **Email**: `participant@hackforge.local`
     - **Password**: `Password123!`
  2. Click **Events** (`/events`), then on **Sample Hack 2026** (`Closed`), navigate to `http://localhost:8080/events/evt_01/submit` to show the red **`Submissions are closed.`** banner and disabled inputs.
  3. Return to `/events` and click **Submit Project** on **HackForge Spring Demo Hackathon 2026** (`/events/evt_open_demo/submit`).
  4. Fill in the submission form for team **Demo Innovators**:
     - **Project Title**: `EdgeCache Zero`
     - **Short Tagline**: `Local-first content-addressed artifact cache`
     - **Track**: `Developer Tools` (`trk_demo_03`)
     - **Summary**: `Deterministic build cache with cryptographic provenance verification.`
     - **Code Repository URL**: `https://github.com/hackforge/edgecache-zero`
     - **Project Thumbnail**: Select your sample `.png` image.
  5. Click **Save Draft** (`action="draft"`). Point out the **`Status: draft (Version 1)`** badge and the rendered thumbnail preview (`/projects/{id}/media/{filename}`).
  6. Click **Submit Final Project** (`action="submit"`).
- **Expected Visible Result**:
  - Closed event `evt_01` strictly refuses new submissions (`HTTP 403` / closed banner).
  - Open event `evt_open_demo` validates and re-encodes the uploaded PNG via Pillow, saves the draft with access-controlled media, increments the optimistic concurrency version on final submission, and redirects to `/projects/{project_id}` where `EdgeCache Zero` is now live on the public gallery.
- **Suggested Narration**:
  > *"Signing in as a participant, we first verify that closed events strictly reject late submissions on the server. Switching to our open demo hackathon, we fill out our team's project, attach a PNG thumbnail validated and re-encoded through Pillow, and save a draft. Draft media is access-controlled to team members and organizers. When we click Submit Final Project, optimistic concurrency increments the version and publishes the project to the gallery."*

---

### `1:30–2:30` — Organizer Dashboard, Rubric Versioning, Judge Invitation & Batch Assignment (`T2`)

- **Action (Browser Window 1)**:
  1. Click **Log Out**, then sign in at `/login` as Organizer:
     - **Email**: `organizer@hackforge.local`
     - **Password**: `Password123!`
  2. Click **Events** (`/events`) $\to$ click **Manage** on **HackForge Spring Demo Hackathon 2026** (`/events/evt_open_demo/manage`).
  3. Scroll to **Judging, Assignments & Rubrics**:
     - Show the **Judging Progress** card (total assignments, finalized, draft, unstarted, under-covered projects) and the **Evaluation Rubric** card (`Demo Rubric v1`).
     - In **Batch Judge Assignment**, keep `Target Reviews Per Project = 3` and click **Run Batch Assignment** (accept confirmation dialog).
  4. Navigate to `/events/evt_01/manage` to show the 30 active judges, track permissions, judging progress (`126` finalized reviews), and recent audit log on the official fixture event.
- **Expected Visible Result**:
  - Batch assignment deterministically assigns eligible track-matched judges to `EdgeCache Zero` while preventing conflicts of interest.
  - `/events/evt_01/manage` displays live progress counters and audit logs.
- **Suggested Narration**:
  > *"Logging in as an event organizer, the management dashboard gives us full control over event windows, tracks, prizes, custom questions, and weighted rubrics. Rubrics automatically snapshot a new version if modified after reviews exist. Running Batch Judge Assignment deterministically distributes submitted projects across track-eligible judges while enforcing conflict-of-interest checks."*

---

### `2:30–3:15` — Judge Queue, Private Scoring & Role Isolation (`T2`)

- **Action (Browser Window 2 — Incognito)**:
  1. Log in at `http://localhost:8080/login` as **Judge Alpha**:
     - **Email**: `judge_a@hackforge.local`
     - **Password**: `Password123!`
  2. Navigate to `http://localhost:8080/events/evt_01/judge` to show Judge Alpha's assigned projects in `evt_01` (`Glass Signal` and `Dry Bridge`, both `Finalized`).
  3. Navigate to `http://localhost:8080/events/evt_01/judges/jdg_01/scores` (own scores JSON $\to$ `200 OK`) and then change the URL to `http://localhost:8080/events/evt_01/judges/jdg_02/scores` (peer judge scores $\to$ `403 Forbidden`).
  4. Also visit `http://localhost:8080/events/evt_01/manage/results` as Judge Alpha $\to$ `403 Forbidden` (judges cannot view organizer results).
- **Expected Visible Result**:
  - Judge Alpha can view and score only their own assigned projects, receives `200 OK` on their own score endpoint, and is blocked with `403 Forbidden` when attempting to inspect Judge Beta's scores or organizer results.
- **Suggested Narration**:
  > *"In an isolated browser session as Judge Alpha, the private judge portal lists only projects assigned to this judge. Judge Alpha can access their own evaluations, but attempting to read another judge's scores (`jdg_02`) or the organizer results dashboard is rejected at the backend with HTTP 403 Forbidden."*

---

### `3:15–4:15` — Organizer Results, Per-Track vs. Diagnostic Cohorts, Snapshots & CSV Exports (`T2`)

- **Action (Browser Window 1 — Organizer)**:
  1. From `/events/evt_01/manage`, click **📊 Results & Normalization** (`/events/evt_01/manage/results`).
  2. Point out the **Comparable Per-Track Cohort Tabs** (`Track: Developer tools`, `Track: Data and analytics`, `Track: Education`, etc.) and the green **`✓ Connected Evaluation Network`** indicator.
  3. Scroll through the **Rankings Table** (`Normalized Index`, `Norm Rank`, `Raw Mean`, `Raw Rank`, `Verified` vs `Provisional` coverage badges) and **Judge Calibration Statistics** ($N, \mu, \sigma$, and `Excluded` badges for judges with $N < 3$ or $\sigma \le 10^{-6}$ such as constant-score judge `jdg_07` Taro Shimizu).
  4. Click the rightmost tab **`🔬 All Tracks (Diagnostic Cross-Track Pooling)`** (`?cohort_id=all:rb_evt_01_v1`) to show the red **`⚠️ Disconnected Evaluation Network Detected (Global Normalized Rank Withheld)`** banner and component-local ranks (`Component 1 #1`, `Component 2 #1`).
  5. Switch back to **`Track: Developer tools`**, click **📸 Take Snapshot**, and point out the **`Viewing Immutable Snapshot`** header and **`✓ SYNCHRONIZED WITH LIVE DATABASE (VERIFIED V2 MANIFEST)`** badge.
  6. Click **📥 Export Judging CSV** and **Judges Calibration** to download `judging.csv` and `judges-calibration.csv`.
- **Expected Visible Result**:
  - Primary per-track cohorts are connected and show full Z-score rankings alongside raw means.
  - Diagnostic cross-track pooling detects the 2 disconnected judge-project components and withholds unqualified global normalized ranks in favor of component-local ranks.
  - Taking a snapshot captures a `REPEATABLE READ` v2 manifest and streams RFC 4180 CSV exports.
- **Suggested Narration**:
  > *"On the Results and Normalization page, HackForge computes judge-wise Z-scores within comparable track-and-rubric cohorts. Judges with fewer than 3 reviews or zero variance—like fixture judge Taro Shimizu who gave identical scores—are automatically excluded from Z-score normalization while their raw scores remain preserved. If an organizer inspects the diagnostic cross-track pooling tab, HackForge's bipartite graph analysis detects two disconnected evaluation components and withholds unqualified global normalized ranks, reporting component-local ranks instead. With one click, we capture a REPEATABLE READ canonical v2 snapshot and export RFC 4180 CSVs."*

---

### `4:15–5:00` — Automated Test Suite & Official Acceptance Checker (`run.py`)

- **Action (Terminal)**:
  ```sh
  python3 run.py .dogfood.toml
  docker compose exec app pytest -q
  ```
- **Expected Visible Result**:
  - `python3 run.py .dogfood.toml` prints all `T1` and `T2` checks passing and concludes with:
    `claimed T1 T2, verified T1 T2`
  - `pytest -q` runs all 100 tests and reports `100 passed`.
- **Suggested Narration**:
  > *"Finally, we run the unmodified official DOGFOOD acceptance checker against `.dogfood.toml`, verifying every T1 and T2 probe with `claimed T1 T2, verified T1 T2`, followed by our 100-test Pytest suite covering PostgreSQL concurrency, snapshot provenance, and role isolation. Thank you!"*

---

## Post-Recording User Checklist

- [ ] Verify the recorded video is **5:00 or under** and audio is clear.
- [ ] Upload the video to your hosting platform and paste the URL into [`README.md`](./../README.md) (`Demo Video` link at the top).
- [ ] Confirm `acceptance-report.txt` is present and unchanged (`python3 run.py .dogfood.toml > acceptance-report.txt`).
