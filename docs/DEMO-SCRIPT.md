# HackForge 5-Minute Live Demo Script (`T1` + `T2`)

**Target Duration**: `4:30 – 4:45` (strictly under the 5:00 cap)  
**Verified Rehearsal Status**: Rehearsed end-to-end against an isolated disposable deployment (`hackforge-rehearsal`, `http://127.0.0.1:18080`) using headless Google Chrome (`Chrome/154.0.8037.57` via CDP). Automated browser + CLI execution completed all 6 segments in **`9.70 seconds`** (`9,695 ms`), leaving comfortable pacing for live narration inside `4:30 – 4:45`. See [`docs/release-evidence/README.md`](./release-evidence/README.md) and [`docs/release-evidence/demo_rehearsal_report.json`](./release-evidence/demo_rehearsal_report.json).

---

## Pre-Recording Setup (Before Pressing Record)

1. Ensure the stack is running and seeded (`docker compose up -d`).
2. Open two browser windows side-by-side at `1280x800` (or `1920x1080` at `125%` zoom):
   - **Window 1 (Main Browser)**: Unauthenticated visitor at `http://localhost:8080/projects` (used for Visitor $\to$ Participant $\to$ Organizer).
   - **Window 2 (Incognito / Secondary Profile)**: At `http://localhost:8080/login` (used for **Judge Alpha**, `judge_a@hackforge.local`).
3. Open a terminal in the repository root with `.dogfood.toml` ready.
4. Keep a small valid PNG file ready on disk (e.g., `docs/release-evidence/rehearsal_02_submitted_project.png`) for the live thumbnail upload in Segment 2.

> **Track & Judge Alignment Note (`evt_open_demo`)**:
> In `app/seed.py`, the open demonstration event (`evt_open_demo`, **HackForge Spring Demo Hackathon 2026**) seeds two judges with distinct track permissions:
> - **Judge Alpha (`jdg_open_a`, `judge_a@hackforge.local`)**: `["trk_demo_01", "trk_demo_02"]` (*Web & Cloud*, *AI & Machine Learning*)
> - **Judge Beta (`jdg_open_b`, `judge_b@hackforge.local`)**: `["trk_demo_02", "trk_demo_03"]` (*AI & Machine Learning*, *Developer Tools*)
>
> Submitting the live demo project to **`AI & Machine Learning` (`trk_demo_02`)** and setting **`Target Reviews = 2`** matches both seeded judges without modifying or expanding judge permissions.

---

## Segment 1 (`0:00 – 0:30`) — Self-Hosted Startup & Public Discovery (`T1`)

### Actions
1. **Terminal**: Show running containers and health probe:
   ```sh
   docker compose ps
   curl -s http://localhost:8080/healthz
   ```
   *(Output: `{"status":"ok","service":"hackforge","database":"connected"}`)*
2. **Window 1 (`/projects`)**:
   - Point out the `41` pre-seeded submitted projects from `fixtures.json` (**Sample Hack 2026**, `evt_01`).
   - Type `Slow Trail` in the search box, select track **`Developer tools` (`trk_01`)**, and click **Search**.
   - Click `/projects/prj_01` (**Glass Signal**) to show the public project detail page (team roster, track badge, repository link, and zero private judge/score leakage).
   - Click **Events** (`/events`) in the top navigation bar to show both **Sample Hack 2026 (`evt_01`, Closed)** and **HackForge Spring Demo Hackathon 2026 (`evt_open_demo`, Open)**.

### Narration
> "HackForge is a self-hosted, offline-capable hackathon portal built with FastAPI and PostgreSQL 17. A single `docker compose up` runs Alembic migrations, acquires a PostgreSQL advisory lock, and seeds the official 41-project `Sample Hack 2026` fixture alongside an open demonstration hackathon. Visitors can search and filter the public gallery by keyword and track without authentication, while draft submissions and private judging scores remain strictly isolated."

---

## Segment 2 (`0:30 – 1:30`) — Participant Deadline Enforcement, Draft Save, PNG Upload & Final Submission (`T1`)

### Actions
1. **Window 1 (`/login`)**:
   - Log in as **Participant**: `participant@hackforge.local` / `Password123!`.
2. **Deadline Refusal Check (`/events/evt_01/submit`)**:
   - Navigate to `http://localhost:8080/events/evt_01/submit`.
   - Highlight the red banner: **`Submissions are closed. The deadline for Sample Hack 2026 has passed.`** All form inputs are disabled and any `POST` returns `403 Forbidden`.
3. **Live Draft + PNG Upload + Final Submission (`/events/evt_open_demo/submit`)**:
   - Navigate to `http://localhost:8080/events/evt_open_demo/submit` (Team: **Demo Innovators**, `tm_open_demo`).
   - Fill out the project form:
     - **Project Title**: `EdgeCache Zero`
     - **Short Tagline**: `Local-first content-addressed artifact cache`
     - **Track**: Select **`AI & Machine Learning`** (`trk_demo_02`)
     - **Summary**: `Deterministic build cache with cryptographic provenance verification.`
     - **Code Repository URL**: `https://github.com/hackforge/edgecache-zero`
     - **Project Thumbnail**: Choose a valid PNG file (`< 5 MB`).
   - Click **Save Draft**.
   - Point out the **`STATUS: DRAFT (VERSION 1)`** optimistic concurrency badge and the rendered thumbnail preview (`<img src="/projects/prj_.../media/...">`), which is access-controlled while the project is in draft.
   - Click **Submit Final Project**.
   - Browser redirects to `/projects/{project_id}`, showing **`EdgeCache Zero`** published under **`AI & MACHINE LEARNING`**.

### Narration
> "Signed in as a participant, attempting to submit to the closed `Sample Hack 2026` event is blocked both in the UI and on the server. Switching to the open `HackForge Spring Demo Hackathon`, we create `EdgeCache Zero` in the `AI & Machine Learning` track (`trk_demo_02`), attach a PNG thumbnail validated by Pillow and served through an access-controlled media route, and save a Version 1 draft tracked with optimistic concurrency control. Clicking `Submit Final Project` transitions the project to `submitted` and publishes it to the public gallery."

---

## Segment 3 (`1:30 – 2:15`) — Organizer Target Coverage & Conflict-Aware Batch Assignment (`T2`)

### Actions
1. **Window 1**:
   - Click **Log Out**, then log in as **Organizer**: `organizer@hackforge.local` / `Password123!`.
2. **Navigate to `/events/evt_open_demo/manage`**:
   - Point out **Tracks (3)** showing `AI & Machine Learning (trk_demo_02)` now has `1` submission, and **Active Judges (2)** showing:
     - `jdg_open_a` (**Judge Alpha**): `TRK_DEMO_01`, `TRK_DEMO_02`
     - `jdg_open_b` (**Judge Beta**): `TRK_DEMO_02`, `TRK_DEMO_03`
   - In **Event Configuration**, set **Target Reviews** (`#target_reviews_per_project`) to **`2`** (matching the 2 judges eligible for `trk_demo_02`) and click **Save Changes**.
   - In **Batch Judge Assignment**, set **Target Reviews Per Project** (`#target_reviews`) to **`2`** and click **Run Batch Assignment**.
   - Point out the updated **Judging Progress** card:
     - **`Total Assignments: 2`** (assigned to both eligible judges on `trk_demo_02`: Judge Alpha and Judge Beta)
     - **`Finalized: 0`** | **`In Draft: 0`** | **`Unstarted: 2`**
     - **`Under-covered Projects: 1`** | **`Judges with outstanding reviews: 2`**

### Narration
> "In the Organizer dashboard for `HackForge Spring Demo Hackathon`, both seeded judges—Judge Alpha and Judge Beta—hold permissions for `trk_demo_02` (`AI & Machine Learning`). We set `Target Reviews Per Project` to `2` to match our two eligible judges and run deterministic batch assignment. The allocator enforces track permissions and team conflict-of-interest rules, creating 2 assignments for `EdgeCache Zero` and updating live Judging Progress to `2 Total Assignments, 0 Finalized, 2 Unstarted`."

---

## Segment 4 (`2:15 – 3:15`) — Live Judge Draft & Finalize on `evt_open_demo`, Organizer Progress Update, Then Switch to `evt_01` for Isolation Checks (`T2`)

### Actions
1. **Window 2 (Judge Alpha Session — `evt_open_demo`)**:
   - Log in at `http://localhost:8080/login` as **Judge Alpha**: `judge_a@hackforge.local` / `Password123!`.
   - Navigate to `http://localhost:8080/events/evt_open_demo/judge`.
   - Point out Judge Alpha's header (`Permitted Tracks: trk_demo_01, trk_demo_02`) and the assigned row for **`EdgeCache Zero`** (`Status: Not Started`, button: **Score Project**).
   - Click **Score Project** (`/events/evt_open_demo/judge/projects/{project_id}/review`).
   - Enter initial draft scores on **Demo Rubric v1** (`1.0 – 5.0`, equal weight `1.0`):
     - **Functionality & Execution**: `4`
     - **Code & Technical Quality**: `4`
     - **Innovation & Originality**: `4`
     - **Judge Feedback & Notes**: `Initial draft notes: checking content-addressed caching provenance.`
   - Click **Save Draft**.
   - Point out the queue row on `/events/evt_open_demo/judge`: status is now **`DRAFT SAVED`**, raw score is **`75.0 / 100`**, and the action button reads **Continue Draft**.
   - *(Optional quick glance at Window 1)*: Refresh `/events/evt_open_demo/manage` to show **`In Draft: 1`**, **`Unstarted: 1`**.
   - Back in **Window 2**, click **Continue Draft**, update **Functionality & Execution** from `4` to **`5`**, update notes to `Strong content-addressed caching design with clear provenance guarantees.`, and click **Finalize Evaluation**.
   - Point out the updated queue row: status is now **`FINALIZED`** and raw score is **`83.33 / 100`**.
2. **Window 1 (Organizer Session — `evt_open_demo`)**:
   - Refresh `http://localhost:8080/events/evt_open_demo/manage`.
   - Highlight the updated **Judging Progress** metrics:
     - **`Total Assignments: 2`**
     - **`Finalized: 1`** (Judge Alpha's completed review)
     - **`In Draft: 0`**
     - **`Unstarted: 1`** (Judge Beta's remaining assignment)
     - **`Under-covered Projects: 1`** (`1` of `2` target reviews finalized; `1` remaining with Judge Beta)
     - **`Judges with outstanding reviews: 1`** (Judge Beta)
3. **Explicit Transition to Official Fixture Event `evt_01` (Window 2 — Judge Alpha)**:
   - Narrate explicitly: *"Now let's switch from our live demo event `evt_open_demo` to the official 41-project fixture event `evt_01` (`Sample Hack 2026`), where Judge Alpha is bound to `jdg_01`, to verify strict peer-score isolation."*
   - In Window 2, open `/events/evt_01/judges/jdg_01/scores` $\to$ returns **`200 OK`** JSON with Judge Alpha's own scores.
   - Change the URL to peer judge `/events/evt_01/judges/jdg_02/scores` $\to$ returns **`403 Forbidden`** (`Judges may only view their own scores`).
   - Navigate to `/events/evt_01/manage/results` in Window 2 $\to$ returns **`403 Forbidden`**.

### Narration
> "In Window 2, Judge Alpha logs in and opens `EdgeCache Zero` in `HackForge Spring Demo Hackathon`. Saving a draft with scores of `4, 4, 4` records `75.0 / 100` (`Draft Saved`) and immediately reflects as `1 In Draft` on the Organizer dashboard. Judge Alpha then reopens the draft, raises `Functionality & Execution` to `5`, and clicks `Finalize Evaluation` for a raw score of `83.33 / 100`. Refreshing the Organizer dashboard shows `1 Finalized` and `1 Unstarted`—accurately reporting that 1 of the 2 target reviews is complete while Judge Beta's review remains outstanding. Next, switching to the official `Sample Hack 2026` fixture event (`evt_01`), Judge Alpha can inspect their own scores on `jdg_01`, but any attempt to access peer judge `jdg_02` or organizer results is blocked with `HTTP 403 Forbidden`."

---

## Segment 5 (`3:15 – 4:15`) — `evt_01` Normalization, Connected vs. Disconnected Cohorts, Immutable Snapshots & CSV Exports (`T2`)

### Actions
1. **Window 1 (Organizer Session — Navigate to `/events/evt_01/manage/results`)**:
   - Explicitly state that we are now inspecting **`Sample Hack 2026` (`evt_01`)** (41 submitted projects, 30 judges, 126 finalized reviews, target coverage = `3`).
   - Show the default primary cohort tab **`Track: Developer tools (6 projects)`** (`trk_01:rb_evt_01_v1`):
     - Point out **`✓ Connected Evaluation Network: All evaluated projects in this cohort are connected by overlapping usable judges.`**
     - Point out **`#1 Slow Trail (`prj_33`)`** (`3 / 3` usable reviews, Normalized Index **`+1.1535`**, Raw Mean **`75.00`**, **`VERIFIED`**) vs **`#3 Flat Relay (`prj_29`)`** (`2 / 3` usable reviews, flagged **`PROVISIONAL`** because usable coverage `2 < 3`).
2. **Switch to `🔬 All Tracks (Diagnostic Cross-Track Pooling)` (`/events/evt_01/manage/results?cohort_id=all:rb_evt_01_v1`)**:
   - Click the dashed **`🔬 All Tracks (Diagnostic Cross-Track Pooling)`** tab.
   - Point out the red connectivity alert:
     - **`⚠️ Disconnected Evaluation Network Detected (Global Normalized Rank Withheld)`**
     - **`Component 1: 20 projects, 10 usable judges`** (`trk_02, trk_04, trk_06, trk_08`) and **`Component 2: 21 projects, 11 usable judges`** (`trk_01, trk_03, trk_05, trk_07`).
     - In the rankings table, global normalized rank is withheld (`—`), and only component-local ranks (`Component 1 #1 Hollow Signal`, `Component 1 #2 Iron Switch`, `Component 2 #1 Slow Trail`) are reported.
   - Scroll down to **Judge Calibration Statistics** on the pooled view:
     - Point out **`Iva Petrova` (`jdg_07`)**: `N = 3`, `Mean = 66.67`, `StdDev = 0.0000`, status **`EXCLUDED`** (`near_zero_variance (pstdev=0.000000e+00 <= 1.000000e-06)`), while her raw scores remain included in project raw means.
     - Point out **`Tomas Varga` (`jdg_01`)**: `N = 1`, status **`EXCLUDED`** (`insufficient_samples (n=1 < 3)`).
3. **Capture an Immutable Snapshot & Export CSVs**:
   - Click back to **`Track: Developer tools`**, then click **`📸 Take Snapshot`**.
   - Point out the header updating to **`Viewing Immutable Snapshot: snp_evt_01_...`** with the green badge **`✓ SYNCHRONIZED WITH LIVE DATABASE (VERIFIED V2 MANIFEST)`** (captured in a single `REPEATABLE READ` transaction).
   - Click **`📥 Export Judging CSV`** (`/events/evt_01/export/judging.csv`) and **`Judges Calibration`** (`/events/evt_01/export/judges-calibration.csv`) to download the RFC 4180 CSV exports.

### Narration
> "Switching the Organizer view to `Sample Hack 2026` (`evt_01`), HackForge normalizes scores within comparable `(track, rubric)` cohorts using within-judge population Z-scores. In `Developer tools`, all 6 projects form a single connected evaluation network led by `Slow Trail` at `+1.1535`, while projects below the target of 3 usable reviews are flagged `Provisional`. Clicking the `Diagnostic Cross-Track Pooling` tab illustrates why per-track cohorts are primary: across the whole event, judges partition into two disconnected components with zero shared usable judges, so HackForge withholds any misleading global normalized rank and reports only component-local ranks. In the Judge Calibration table, `Iva Petrova` (`jdg_07`), who gave identical `66.67` scores across all 3 reviews, is automatically excluded from Z-score normalization for zero variance while her raw scores are preserved, and `Tomas Varga` (`jdg_01`, $N=1 < 3$) is excluded for insufficient sample size. Finally, we freeze an immutable `REPEATABLE READ` v2 snapshot with staleness verification and export the results and judge calibration tables as CSV."

---

## Segment 6 (`4:15 – 4:45`) — Unmodified Official Acceptance Checker (`run.py`)

### Actions
1. **Terminal**: Run the unmodified official acceptance checker against the live stack:
   ```sh
   python3 run.py .dogfood.toml
   ```
2. Show the output:
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

### Narration
> "To close, we run the unmodified DOGFOOD 2026 acceptance checker `python3 run.py .dogfood.toml` against the live portal. All seven T1 and T2 acceptance probes pass with exit code 0: `claimed T1 T2, verified T1 T2`. Thank you!"
