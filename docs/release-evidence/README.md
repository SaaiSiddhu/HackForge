# Release Verification & Demo Rehearsal Evidence Index

This directory preserves a curated, sanitized subset of browser screenshots, automated execution reports, and the Chrome DevTools Protocol (CDP) rehearsal script used to verify HackForge for **DOGFOOD 2026**.

---

## 1. Environment, Browser & Viewport Metadata

- **Browser Engine**: Headless Google Chrome `Chrome/154.0.8037.57` (`V8 15.4.80.11`, CDP Protocol `1.3`) driven via native WebSocket Chrome DevTools Protocol with isolated `BrowserContext` sessions per user role.
- **Viewports Tested**:
  - **Desktop**: `1280x800` (all rehearsal and walkthrough screenshots, plus horizontal overflow audit across 16 routes).
  - **Mobile**: `375x812` (horizontal overflow audit across 16 routes: `0` pages with `scrollWidth > clientWidth`).
- **Disposable Verification Deployments**:
  - `hackforge-rehearsal` (`http://127.0.0.1:18080`, fresh PostgreSQL 17 and upload volumes, torn down after rehearsal) — tested against commit `70f2e34`.
  - `hackforge-clean` (`http://127.0.0.1:18080`, clean `git checkout-index` export with fresh volumes, torn down after verification) — tested during Phase 5 browser verification.
  - The normal local application (`hackforge-app-1` on `127.0.0.1:8080` and `hackforge-db-1`) and all unrelated containers were left untouched throughout.

---

## 2. Sanitization & Security Audit of Preserved Artifacts

Before committing any file in this directory, every JSON report, script, and PNG screenshot was inspected for sensitive data:
1. **Redacted Tokens & Local Debugger URLs**:
   - In [`chrome_walkthrough_report.json`](./chrome_walkthrough_report.json), one-time cryptographic invitation paths (`steps.step05_teams.teamInviteHref` and `steps.step09_judge_invite.judgeInviteUrl`) and the local Chrome DevTools `browser.webSocketDebuggerUrl` were redacted (`[REDACTED_TEAM_INVITE_TOKEN]`, `[REDACTED_JUDGE_INVITE_TOKEN]`, and removed `webSocketDebuggerUrl`).
   - In [`demo_rehearsal_report.json`](./demo_rehearsal_report.json), `webSocketDebuggerUrl` is omitted by construction and no invitation tokens or session tokens are logged.
   - In [`rehearse_demo_script.mjs`](./rehearse_demo_script.mjs), host-specific paths were replaced with portable `import.meta.dirname` relative resolution.
2. **Visual Screenshot Inspection**:
   - All 10 PNG screenshots were visually inspected to confirm they show only pre-seeded demo/fixture identities (`Demo Participant`, `Demo Organizer`, `Judge Alpha`, and `Sample Hack 2026` fixture records) and contain no session cookies, raw invitation URLs, passwords, or private host data.
   - The earlier pre-`70f2e34` judge queue capture (`11_judge_finalized.png`, taken during the intermediate verification pass before `judge_dashboard.html` badge contrast cleanup) was excluded and superseded by [`rehearsal_04_judge_alpha_finalized.png`](./rehearsal_04_judge_alpha_finalized.png), which matches the final `70f2e34` UI.

---

## 3. Artifact Inventory

### A. Final 5-Minute Demo Rehearsal (`70f2e34`, `hackforge-rehearsal`)

| File | Segment / Route | What It Verifies |
| :--- | :--- | :--- |
| [`demo_rehearsal_report.json`](./demo_rehearsal_report.json) | All 6 Demo Segments (`docs/DEMO-SCRIPT.md`) | Machine-readable report of the complete end-to-end rehearsal (`PASS` on all 6 segments; **`9.70s` (`9,695 ms`)** total automated execution; `4:15 – 4:45` human-narrated pacing). |
| [`rehearse_demo_script.mjs`](./rehearse_demo_script.mjs) | CDP Automation Runner | Reproducible Node.js + Chrome DevTools Protocol script that executes every step of [`docs/DEMO-SCRIPT.md`](../DEMO-SCRIPT.md) and asserts UI/API state. |
| [`rehearsal_01_participant_draft.png`](./rehearsal_01_participant_draft.png) | Segment 2 · `/events/evt_open_demo/submit` | Participant saves draft for **`EdgeCache Zero`** in **`AI & Machine Learning` (`trk_demo_02`)** with PNG thumbnail upload; shows **`STATUS: DRAFT (VERSION 1)`**. |
| [`rehearsal_02_submitted_project.png`](./rehearsal_02_submitted_project.png) | Segment 2 · `/projects/prj_abd816442d` | Finalized submission detail view for **`EdgeCache Zero`** published under **`AI & MACHINE LEARNING`** by team **Demo Innovators**. |
| [`rehearsal_03_organizer_assigned.png`](./rehearsal_03_organizer_assigned.png) | Segment 3 · `/events/evt_open_demo/manage` | Organizer sets `Target Reviews = 2` and runs batch assignment on `evt_open_demo`; shows **`2 TOTAL ASSIGNMENTS`**, **`0 FINALIZED`**, **`0 IN DRAFT`**, **`Unstarted: 2`**, and active judges `jdg_open_a` (`TRK_DEMO_01`, `TRK_DEMO_02`) and `jdg_open_b` (`TRK_DEMO_02`, `TRK_DEMO_03`). |
| [`rehearsal_04_judge_alpha_finalized.png`](./rehearsal_04_judge_alpha_finalized.png) | Segment 4 · `/events/evt_open_demo/judge` | Eligible **Judge Alpha** (`jdg_open_a`, `judge_a@hackforge.local`) queue after saving draft (`75.0 / 100`) and finalizing evaluation (`5, 4, 4` $\to$ **`FINALIZED`**, **`83.33 / 100`**) on **`EdgeCache Zero`**. |
| [`rehearsal_05_organizer_progress_updated.png`](./rehearsal_05_organizer_progress_updated.png) | Segment 4 · `/events/evt_open_demo/manage` | Organizer dashboard refreshed after Judge Alpha finalizes: **`2 TOTAL ASSIGNMENTS`**, **`1 FINALIZED`**, **`0 IN DRAFT`**, **`Unstarted: 1`** (Judge Beta remaining), **`Under-covered Projects: 1`**, **`Judges with outstanding reviews: 1`**. |
| [`rehearsal_06_diagnostic_disconnected_cohort.png`](./rehearsal_06_diagnostic_disconnected_cohort.png) | Segment 5 · `/events/evt_01/manage/results?cohort_id=all:rb_evt_01_v1` | Official fixture `evt_01` diagnostic cross-track pooling view showing **`⚠️ Disconnected Evaluation Network Detected (Global Normalized Rank Withheld)`**, `Component 1: 20 projects, 10 usable judges`, `Component 2: 21 projects, 11 usable judges`, and component-local ranks (`Component 1 #1 Hollow Signal`, `Component 1 #2 Iron Switch`, `Component 1 #3 Salt Ledger`). |

### B. Phase 5 14-Step Browser & Viewport Verification (`hackforge-clean`)

| File | Step / Route | What It Verifies |
| :--- | :--- | :--- |
| [`chrome_walkthrough_report.json`](./chrome_walkthrough_report.json) | Steps 1–14 + Viewport (`1280x800`, `375x812`) + A11y Audit | Full report of the 14 browser journeys (including team invite acceptance, custom event creation, judge invite acceptance, all 6 CSV exports, role denials, `0` viewport overflows, `0` unlabeled inputs, and `6` `:focus-visible` keyboard focus stops). |
| [`01_gallery_home.png`](./01_gallery_home.png) | Step 1 · `/projects` | Public gallery displaying the `41` pre-seeded submitted projects from `fixtures.json` (`Sample Hack 2026`, `evt_01`). |
| [`12_organizer_results_live.png`](./12_organizer_results_live.png) | Step 12 · `/events/evt_01/manage/results` | Primary per-track comparable cohort (`Track: Developer tools`, `trk_01:rb_evt_01_v1`) showing **`✓ Connected Evaluation Network`** and `#1 Slow Trail` (`+1.1535`, `75.00`, `VERIFIED`). |
| [`12b_organizer_results_snapshot.png`](./12b_organizer_results_snapshot.png) | Step 12b · `/events/evt_01/manage/results?snapshot_id=...` | Immutable snapshot view after clicking `📸 Take Snapshot`, showing **`Viewing Immutable Snapshot:`** and **`✓ Synchronized with live database (Verified v2 Manifest)`**. |
| [`14_role_denial_403.png`](./14_role_denial_403.png) | Step 14 · `/events/evt_01/judge` (as non-judge) | Styled `403 Access Denied` response when a participant or unauthorized user attempts to access judge or organizer routes. |

---

## 4. Known Limitations of Automated Browser Evidence

- **Headless Rendering vs. Live Video Recording**: Screenshots and timings in [`demo_rehearsal_report.json`](./demo_rehearsal_report.json) were captured via headless Chrome (`--headless=new`) over CDP. While the automated flow completes in `9.70s`, a human presenter recording the final 5-minute submission video should pace clicks and narration across `4:30 – 4:45` as scripted in [`docs/DEMO-SCRIPT.md`](../DEMO-SCRIPT.md).
- **Single-Reviewer Live Demo vs. Multi-Reviewer Fixture Calibration**: In the live `evt_open_demo` walkthrough, only Judge Alpha completes a review on `EdgeCache Zero` (`1` of `2` target reviews finalized) to keep the demo concise; statistical Z-score calibration ($N_j \ge 3$, $\sigma_j > 10^{-6}$, connected vs. disconnected components, and `REPEATABLE READ` snapshots) is therefore demonstrated on the rich 41-project, 30-judge, 126-review `evt_01` fixture immediately afterward.
