# Phase 5 Release Verification & Submission Packaging Report

**Repository**: `/Users/saai_siddharth/Projects/Hackathon/HackForge`  
**Starting Commit**: `0f32e1dca1276d87af5c4a16fa9204c0f41a7123`  
**Claimed & Verified Tiers**: `T1`, `T2`

---

## 1. Executive Verification Matrix

| Verification Area | Status | Method & Evidence |
| :--- | :---: | :--- |
| **1. Normal Application Preservation** | **PASS** | `hackforge-app-1` (`127.0.0.1:8080`), `hackforge-db-1`, `hackforge_database`, and `hackforge_uploads` preserved without reset or data loss; all unrelated containers untouched. |
| **2. Default Package Runnable (`docker compose up`)** | **PASS** | `docker-compose.yml` runs self-contained from image without source bind-mount (`.:/portal` moved to optional `docker-compose.dev.yml`), without host port `5432` exposure, and with `Dockerfile` installing strictly from `requirements.lock` (`pip install --no-cache-dir --no-deps -r requirements.lock`). |
| **3. Clean-Clone Installation & Persistence** | **PASS** | Tracked files exported via `git checkout-index -a -f --prefix=/tmp/hackforge-clean-export/`, started on `APP_PORT=18080` (`hackforge-clean`) with fresh volumes. Verified `alembic current -> 0005 (head)`, non-root `uid=1000(portal)` `/portal/uploads` ownership, field-by-field fixture fidelity (41 projects including `prj_07`/`prj_41`, 40 teams, 30 judges, 8 tracks, 126 reviews), restart persistence (`AFTER_RESTART_PERSISTENCE: PASS`), and repeated seeding idempotence (`0` rows inserted, no overwrites). |
| **4. Step-by-Step Migration Upgrade (`0001 -> 0005`)** | **PASS** | Verified sequential upgrades `0001 -> 0002 -> 0003 -> 0004 -> 0005` on disposable database `hackforge_upgrade_test` (`UPGRADE_PATH_0001_TO_0005_VERIFIED`). |
| **5. Air-Gapped Offline Runtime Verification** | **PASS** | Ran `hackforge-offline` with `pull_policy: never` on an `internal: true` Docker bridge network. Verified inside container that `1.1.1.1:443` raised `OSError(101, 'Network is unreachable')` and `https://example.org` raised `gaierror(-3, 'Temporary failure in name resolution')`, while all 7 application capabilities succeeded offline. |
| **6. Real Browser Walkthrough (`Chrome/154.0.8037.57`)** | **PASS** | Driven via Chrome DevTools Protocol (CDP) across isolated browser contexts for all 14 user journeys (`step01` through `step14`), plus `1280x800` desktop and `375x812` mobile viewport checks (`0` overflow pages), `0` unlabeled form controls, and `6` verified `:focus-visible` keyboard focus transitions. |
| **7. Automated Test Suite (`pytest -v`)** | **PASS** | **100 passed, 0 failed, 0 skipped** across 15 test modules against isolated `hackforge_test` PostgreSQL and temporary SQLite/upload fixtures. |
| **8. Official Acceptance Checker (`run.py .dogfood.toml`)** | **PASS** | Unmodified `python3 run.py .dogfood.toml` exits `0` with `claimed T1 T2, verified T1 T2`. |

---

## 2. Concrete Release-Blocking Defects Discovered & Fixed in Phase 5

1. **`NameError: name 'get_event_or_404' is not defined` on Batch Judge Assignment (`app/routers/judging.py`)**:
   - **Discovery**: Exercising `POST /events/{event_id}/manage/assignments/batch` from the Organizer dashboard raised a `NameError` because `organizer_batch_assignments` called `get_event_or_404(db, event_id)`, which was not defined in `app/routers/judging.py`.
   - **Fix**: Defined `get_event_or_404(db: Session, event_id: str) -> Event` in `app/routers/judging.py` and added an explicit HTTP regression test (`test_organizer_batch_assignment_http_endpoint` in `tests/test_judging.py`).
2. **Mobile Viewport (`375px`) Horizontal Overflow on Multi-Column Grid Pages (`app/static/style.css`)**:
   - **Discovery**: Real Chrome CDP testing at `375x812` detected horizontal overflow on `/events/{id}/manage` (`scrollWidth: 509 > 375`) and `/events/{id}/manage/results` (`scrollWidth: 498 > 375`) because inline multi-column grids (`grid-template-columns: 1fr 1fr`) and `.card` containers lacked `minmax(0, 1fr)` and `min-width: 0`, allowing wide tables to stretch grid tracks past the viewport.
   - **Fix**: Added `@media (max-width: 768px)` rules in `app/static/style.css` collapsing inline grids to `minmax(0, 1fr) !important` with `min-width: 0; max-width: 100%` on grid children and `.card`, and defined `--bg-surface`, `--text-color`, and `--text-main` in `:root`. Re-verified in Chrome (`0` overflow pages across all 16 screens).
3. **Dark-Theme Contrast & Form Accessibility Labels (`app/templates/*.html`)**:
   - **Discovery**: Template audit and browser inspection identified light-theme inline backgrounds (`#f8fafc`) in `judge_dashboard.html`, `judge_invite_accept.html`, and `judge_review.html`, low-contrast dark-red text in `event_results.html`, a misleading `"All Tracks"` label when `j.tracks` was empty in `event_manage.html`, and two form controls missing explicit labels (`input[readonly]` for generated judge invite URLs in `event_manage.html` and `select#snapshot_id` in `event_results.html`).
   - **Fix**: Replaced inline light backgrounds with dark-theme CSS variables, updated `event_manage.html` to show `All Tracks (*)` only when `'*' in j.tracks` and `No Tracks` when empty, and added `aria-label` attributes to both controls (`0` unlabeled controls across all templates).
4. **Self-Contained Default Packaging & Seed Idempotence (`docker-compose.yml`, `Dockerfile`, `app/seed.py`)**:
   - **Fix**: Removed host source bind mount (`- .:/portal`) and host PostgreSQL port publishing (`5432`) from `docker-compose.yml` (moving them to `docker-compose.dev.yml`), updated `Dockerfile` to install strictly from `requirements.lock` (`--no-deps`), added `/healthz` alias in `app/main.py`, and updated `app/seed.py` to honor `SEED_FIXTURES=false`, preserve existing `Judge.account_id` links and unexpired demo sessions across restarts, and log demo credentials (`Password123!`) when `ENABLE_DEMO_PROVISIONING=true`.

---

## 3. Clean-Clone & Upgrade Path Verification Details

1. **Clean Export**:
   - Exported tracked files only into `/tmp/hackforge-clean-export` using `git checkout-index -a -f --prefix=/tmp/hackforge-clean-export/`.
   - Started `APP_PORT=18080 docker compose -p hackforge-clean up -d --build`.
2. **Fixture Fidelity & Non-Root Volume Ownership**:
   - Verified `id` inside container: `uid=1000(portal) gid=1000(portal)`.
   - Verified `/portal/uploads`: `drwxr-xr-x 2 portal portal` without manual `chmod` or `chown`.
   - Verified `alembic current`: `0005 (head)`.
   - Verified exact match against `fixtures.json`: `1` event (`evt_01`, `"Sample Hack 2026"`), `8` tracks, `40` teams, `30` judges, `41` submitted projects (including both `prj_07` and `prj_41` titled `"Dry Harbour"`), and `126` finalized reviews (including constant-score judge `jdg_07`).
3. **Persistence & Seeding Idempotence**:
   - Saved a draft project and uploaded a PNG image, ran `docker compose -p hackforge-clean restart app`, and confirmed both the database record and `/projects/{id}/media/{filename}` image persisted across restart.
   - Re-ran `python -m app.seed` twice and confirmed `0` rows were inserted or overwritten and existing judge `account_id` bindings and session expiries were preserved.
4. **Sequential Migration Upgrade (`0001 -> 0005`)**:
   - Created disposable database `hackforge_upgrade_test` and executed `alembic upgrade 0001`, `0002`, `0003`, `0004`, and `0005` sequentially, verifying schema state at each revision.

---

## 4. Offline Runtime Verification Details (`internal: true` Network)

1. **Network Isolation Proof**:
   - Started `hackforge-offline` using `docker-compose.offline.yml` with `pull_policy: never` and `networks: default: internal: true`.
   - Verified from inside `hackforge-offline-app-1`:
     - Outbound TCP socket to `1.1.1.1:443`: `OSError(101, 'Network is unreachable')`.
     - Outbound HTTPS/DNS request to `https://example.org`: `URLError(gaierror(-3, 'Temporary failure in name resolution'))`.
2. **Capabilities Verified Offline**:
   - Startup & `/healthz` (`200 OK`, `{"status":"ok","service":"hackforge","database":"connected"}`).
   - Public gallery `/projects` (`200 OK`, `85,947` bytes) and `/static/style.css` (`200 OK`, `0` external URLs).
   - Cookie + CSRF login (`POST /login` -> `302 Found`).
   - Draft save (`POST /events/evt_open_demo/submit`) and PNG upload (`GET /projects/{id}/media/{file}` -> `200 OK`, `image/png`).
   - Batch judge assignment (`POST /events/evt_open_demo/manage/assignments/batch`) and finalized rubric review (`POST /events/evt_open_demo/judge/projects/{id}/review` -> `raw_score: 91.67`).
   - Organizer results calculation (`GET /events/evt_01/manage/results`) and `REPEATABLE READ` snapshot creation (`POST /events/evt_01/manage/results/snapshots`).
   - All 6 CSV exports (`judging.csv`, `reviews.csv`, `assignments.csv`, `teams.csv`, `projects.csv`, `judges-calibration.csv` -> all `200 OK`, `text/csv; charset=utf-8`).

---

## 5. Real Browser Walkthrough Details (`Chrome/154.0.8037.57` via CDP)

Driven against `http://127.0.0.1:18080` (`hackforge-clean`) using headless `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` (`Chrome/154.0.8037.57`, WebSocket CDP protocol `1.3`) with isolated `BrowserContext` sessions per user role:

| Step | Journey | Result | Verified Details |
| :---: | :--- | :---: | :--- |
| **1** | Public gallery & search/track filter | **PASS** | `/` redirects to `/projects`; `/projects?q=Lattice&track=trk_01` filters submitted projects accurately. |
| **2** | Event overview & track view | **PASS** | `/events` renders active and closed hackathons with status badges and deadlines. |
| **3** | Registration & validation alerts | **PASS** | Invalid login renders `.alert-danger`; `/register` creates account and redirects with active session. |
| **4** | Login and logout | **PASS** | CSRF-protected `POST /logout` revokes session; `POST /login` re-authenticates. |
| **5** | Team creation, invitation & acceptance | **PASS** | Captain creates team on `/events/evt_open_demo/teams/new`, invites teammate email; second browser context accepts `/invitations/{token}` and both members render on `/teams/{id}`. |
| **6** | Closed deadline check, draft save, PNG upload & final submission | **PASS** | `/events/evt_01/submit` shows `"Submissions are closed."` banner; on `/events/evt_open_demo/submit`, PNG upload (`DOM.setFileInputFiles`), `Save Draft`, thumbnail preview (`img[src^="/projects/"]`), and `Submit Final Project` succeed. |
| **7** | Public gallery visibility | **PASS** | Unauthenticated browser context finds newly submitted project on `/projects?q=...` and `/projects/{id}` with zero private judging data leaked. |
| **8** | Organizer event, track & rubric setup | **PASS** | Organizer creates event on `/events/new`, adds track, adds weighted rubric criterion, and opens `/events/evt_open_demo/manage`. |
| **9** | Judge invitation & acceptance | **PASS** | Organizer generates one-time invite link on `/events/evt_open_demo/manage`; isolated judge browser context registers and accepts `/events/evt_open_demo/judges/invite/{token}`. |
| **10** | Batch judge assignment | **PASS** | Organizer runs `Run Batch Assignment` (`POST /events/evt_open_demo/manage/assignments/batch`), assigning eligible judges without conflicts of interest. |
| **11** | Judge queue, draft review & finalized review | **PASS** | Judge views assigned project in `/events/evt_open_demo/judge`, saves draft scores (`Save Draft`), then finalizes evaluation (`Finalize Evaluation`); status updates to `Finalized`. |
| **12** | Organizer progress, results & snapshot creation | **PASS** | Organizer views `Judging Progress`, inspects `/events/evt_01/manage/results` (`Viewing Live Database Results`), captures snapshot (`📸 Take Snapshot`), and verifies `Viewing Immutable Snapshot:` with `✓ SYNCHRONIZED WITH LIVE DATABASE (VERIFIED V2 MANIFEST)` and calibration diagnostics. |
| **13** | All 6 CSV exports | **PASS** | Organizer session fetches `judging.csv` (7 lines), `reviews.csv` (127 lines), `assignments.csv` (127 lines), `teams.csv` (44 lines), `projects.csv` (42 lines), and `judges-calibration.csv` (271 lines) — all `200 OK` (`text/csv; charset=utf-8`). |
| **14** | Role denial checks from UI | **PASS** | Participant browser context visiting `/events/evt_01/manage`, `/events/evt_01/manage/results`, and `/events/evt_01/judge` receives styled `403` error page. |
| **A11y & Viewport** | Desktop (`1280x800`) & Mobile (`375x812`) + Labels + Keyboard Focus | **PASS** | `0` horizontal overflow pages across all 16 screens (`scrollWidth <= clientWidth`), `0` unlabeled controls, and `6` keyboard `Tab` focus stops with `2px solid rgb(185, 245, 106)` `:focus-visible` outline. |

---

## 6. Preserved Release Evidence & Final Demo Rehearsal

- **Preserved Artifacts Index**: See [`docs/release-evidence/README.md`](./release-evidence/README.md) for the sanitized screenshots, `chrome_walkthrough_report.json`, `demo_rehearsal_report.json`, and `rehearse_demo_script.mjs`.
- **5-Minute Demo Rehearsal**: See [`docs/DEMO-SCRIPT.md`](./DEMO-SCRIPT.md) for the corrected, end-to-end rehearsed script (`EdgeCache Zero` submitted to `AI & Machine Learning` / `trk_demo_02` on `evt_open_demo` with `Target Reviews = 2`, live draft and finalized evaluation by Judge Alpha `jdg_open_a`, organizer progress update, and explicit transition to `evt_01` for peer-score denial, connected vs. disconnected cohort normalization, `REPEATABLE READ` v2 snapshots, CSV exports, and `run.py`).
- **Submission & Handoff Checklist**: See [`docs/SUBMISSION-CHECKLIST.md`](./SUBMISSION-CHECKLIST.md).

