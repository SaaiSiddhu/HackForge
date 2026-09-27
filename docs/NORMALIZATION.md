# Score Normalization & Statistical Calibration Methodology

## 1. Overview & Cohort Policy

HackForge implements a deterministic judge-wise Z-score calibration engine (`method="zscore_v1"`, `cohort_policy="per_track_comparable_v1"`) to standardize scores within comparable evaluation cohorts while enforcing strict input validation, unified review eligibility, and bipartite connectivity checks.

### Primary Comparable Cohorts vs. Diagnostic Cross-Track Pooling
1. **Default Primary Cohorts — `(event_id, track_id, rubric_id)`**:
   - By default, HackForge partitions submitted projects and eligible reviews by `(event_id, track_id, rubric_id)`.
   - In the official `evt_01` ("Sample Hack 2026") fixture, judges are restricted to 1–2 tracks (`judge.tracks`). Grouping by `(track_id, rubric_id)` produces 8 primary track cohorts (`trk_01:rb_evt_01_v1` through `trk_08:rb_evt_01_v1`), each of which forms a **single connected evaluation network** (`component_count = 1`, `is_connected = True`).
2. **Explicit Diagnostic Option — Whole-Event Cross-Track Pooling (`track_id="all"` / `all:<rubric_id>`)**:
   - Organizers can explicitly select the `all:<rubric_id>` cohort (`is_diagnostic_pooling = True`) to pool a judge's evaluations across multiple tracks that share the same rubric version.
   - **Justification**: When a judge evaluates projects across 2 tracks (e.g., 2 projects in one track and 1 in another), within-track sample size may be $N_{j,\text{track}} < 3$, whereas pooling across tracks under the shared rubric yields $N_{j,\text{all}} \ge 3$.
   - **Limitation & Disconnected Component Policy**: Because judges in `evt_01` only evaluate 1–2 tracks, the pooled whole-event graph partitions into **2 disconnected components** (`Component 1` covering tracks `trk_02, trk_04, trk_06, trk_08` and `Component 2` covering tracks `trk_01, trk_03, trk_05, trk_07`) with zero shared usable judges between them.
   - Whenever a cohort's usable evaluation network has `component_count > 1` (`is_connected = False`), HackForge **withholds any unqualified global `normalized_rank` (`normalized_rank = None`)** and reports only component-local ranks (`component_normalized_rank`: `Component 1 #1..#20`, `Component 2 #1..#21`).

---

## 2. Mathematical Formulation & Input Validation

### Step 1: Raw Score Recalculation & Strict Input Validation
Every review is dynamically validated and recalculated from its stored criterion scores (`ReviewCriterionScore`) against the historical rubric version (`review.rubric_id`) bound to that review:

$$\text{Raw Score}_{j,k} = 100 \times \frac{\sum_{i=1}^{C} w_i \cdot \frac{s_{j,k,i} - \min_i}{\max_i - \min_i}}{\sum_{i=1}^{C} w_i}$$

- **Strict Validation Rules (No Silent Fallbacks)**:
  - Every criterion weight $w_i$ must be finite, non-boolean, and non-negative ($w_i \ge 0$).
  - Total rubric weight $\sum w_i$ must be finite and strictly positive ($\sum w_i > 0$).
  - Every criterion must have finite $\min_i$ and $\max_i$ with $\max_i > \min_i$ (zero or negative ranges are rejected with `ValueError` / `invalid_range_criterion_<key>`; the engine never substitutes `1.0`).
  - Unknown criterion keys not defined in the bound rubric are rejected (`unknown_criterion_<key>`).
  - Every required criterion score $s_{j,k,i}$ must be present, non-boolean, finite (`NaN` and `±inf` rejected), and within $[\min_i, \max_i]$.
  - Normalization parameters are strictly validated: `min_sample` must be an integer $\ge 1$, `epsilon` must be a finite float $> 0.0$, and `method` must be in `{"zscore_v1"}`.
- **Precision**: Full IEEE 754 floating-point precision is preserved through all intermediate calculations and rankings; rounding to 2 or 4 decimal places occurs only at presentation/CSV serialization.

### Step 2: Unified Review Eligibility & Revocation Policy
Both results calculation (`app/scoring_services.py:evaluate_review_eligibility`) and CSV exports (`app/export_services.py`) reuse the centralized track permission policy (`app/judging_services.py:is_judge_permitted_for_track`):
- `["*"]` explicitly permits all tracks.
- Explicit track IDs (e.g., `["trk_01", "trk_03"]`) permit only those tracks.
- `[]` or `None` permits **no tracks**.
- **Historical Revocation Policy**: Under HackForge's live scoring policy, revoking a judge's track permissions (removing a track or setting `judge.tracks = []`) invalidates that judge's historical reviews for affected tracks across live results, diagnostics (`excluded_reviews`), and live CSV exports (`ineligibility_reason = "track_mismatch"`). Immutable snapshots captured prior to revocation preserve the historical eligibility state recorded in their canonical v2 manifest, while `check_snapshot_staleness` flags the snapshot as stale once live judge permissions change.

### Step 3: Per-Judge Cohort Statistics & Guardrails
Within each cohort, let $S_j = [S_{j,1}, S_{j,2}, \dots, S_{j,N_j}]$ be the eligible recalculated raw scores submitted by judge $j$:

$$\mu_j = \frac{1}{N_j} \sum_{k=1}^{N_j} S_{j,k}, \qquad \sigma_j = \sqrt{\frac{1}{N_j} \sum_{k=1}^{N_j} (S_{j,k} - \mu_j)^2}$$

where $\sigma_j$ is the **population standard deviation** (`statistics.pstdev`).
1. **Small Sample Guard ($N_j < 3$)**: Judges with $N_j < \text{min\_sample}$ (`3`) are marked `is_usable = False` (`insufficient_samples`).
2. **Near-Zero Variance Guard ($\sigma_j \le 10^{-6}$)**: Judges with $\sigma_j \le \epsilon$ (`1e-6`) are marked `is_usable = False` (`near_zero_variance`).
3. **Raw Score Preservation**: Unusable judges are excluded only from Z-score normalization; their eligible raw scores remain included in `raw_mean` and `raw_rank`.

### Step 4: Z-Score Transformation & Provisional Coverage Flagging
For each eligible evaluation by a usable judge $j$ on project $k$:

$$z_{j,k} = \frac{S_{j,k} - \mu_j}{\sigma_j}, \qquad \text{Normalized Index}_k = \frac{1}{|Z_k|} \sum_{z \in Z_k} z$$

- If $|Z_k| < \text{event.target\_reviews\_per\_project}$ (`3` in `evt_01`), the project is flagged `is_provisional = True`.
- If $|Z_k| = 0$, `normalized_index`, `normalized_rank`, and `component_normalized_rank` are `None` (serialized as `""` in CSV, never fabricated as `0`).

---

## 3. Primary Per-Track Comparable Cohort Results (`evt_01`)

Across `evt_01` (41 submitted projects, 30 judges, 126 finalized reviews, 0 excluded reviews), all 8 primary `(track_id, rubric_id)` cohorts form single connected components (`is_connected = True`, `component_count = 1`).

### Track `trk_01`: Developer tools (6 Projects, 16 Reviews, 3 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_33` | Slow Trail | 3 / 3 (3 tot) | `+1.1535` | `75.00` | Verified |
| **#2** | #2 | `prj_31` | Salt Ferry | 3 / 3 (3 tot) | `+0.2998` | `63.89` | Verified |
| **#3** | #4 | `prj_29` | Flat Relay | 2 / 3 (2 tot) | `-0.0778` | `58.33` | **Provisional** |
| **#4** | #3 | `prj_32` | Loud Ledger | 3 / 3 (3 tot) | `-0.1253` | `61.11` | Verified |
| **#5** | #5 | `prj_06` | Dry Compass | 3 / 3 (3 tot) | `-0.7012` | `52.78` | Verified |
| **#6** | #6 | `prj_40` | Slow Loom | 2 / 3 (2 tot) | `-0.8624` | `50.00` | **Provisional** |

### Track `trk_02`: Data and analytics (6 Projects, 19 Reviews, 4 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_11` | Salt Ledger | 4 / 3 (4 tot) | `+0.8997` | `83.33` | Verified |
| **#2** | #2 | `prj_25` | Dry Relay | 3 / 3 (3 tot) | `+0.6095` | `77.78` | Verified |
| **#3** | #3 | `prj_21` | Copper Kiln | 3 / 3 (3 tot) | `+0.2621` | `72.22` | Verified |
| **#4** | #4 | `prj_38` | Deep Beacon | 3 / 3 (3 tot) | `+0.0318` | `69.44` | Verified |
| **#5** | #5 | `prj_28` | Flat Meadow | 3 / 3 (3 tot) | `-0.8132` | `61.11` | Verified |
| **#6** | #6 | `prj_05` | North Compass | 3 / 3 (3 tot) | `-1.2897` | `47.22` | Verified |

### Track `trk_03`: Accessibility (6 Projects, 19 Reviews, 4 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_41` | Dry Harbour | 4 / 3 (4 tot) | `+0.2105` | `70.83` | Verified |
| **#2** | #2 | `prj_02` | Small Meadow | 2 / 3 (3 tot) | `+0.1474` | `63.89` | **Provisional** |
| **#3** | #3 | `prj_39` | Paper Anchor | 2 / 3 (2 tot) | `+0.1173` | `62.50` | **Provisional** |
| **#4** | #6 | `prj_07` | Dry Harbour | 3 / 3 (3 tot) | `+0.0124` | `58.33` | Verified |
| **#5** | #3 | `prj_24` | Glass Beacon | 2 / 3 (4 tot) | `-0.0649` | `62.50` | **Provisional** |
| **#6** | #5 | `prj_03` | Deep Compass | 3 / 3 (3 tot) | `-0.4264` | `58.33` | Verified |

### Track `trk_04`: Security (5 Projects, 16 Reviews, 3 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_16` | Salt Kiln | 2 / 3 (3 tot) | `+1.1834` | `75.00` | **Provisional** |
| **#2** | #3 | `prj_15` | Copper Orbit | 2 / 3 (3 tot) | `+0.1336` | `66.67` | **Provisional** |
| **#3** | #2 | `prj_08` | North Drift | 3 / 3 (5 tot) | `-0.0913` | `70.00` | Verified |
| **#4** | #4 | `prj_01` | Glass Signal | 1 / 3 (3 tot) | `-1.1355` | `61.11` | **Provisional** |
| **#5** | #5 | `prj_22` | Dry Bridge | 1 / 3 (2 tot) | `-1.2247` | `52.78` | **Provisional** |

### Track `trk_05`: Climate (3 Projects, 11 Reviews, 2 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_35` | Warm Beacon | 2 / 3 (5 tot) | `+0.5345` | `61.67` | **Provisional** |
| **#2** | #2 | `prj_13` | Quiet Anchor | 2 / 3 (3 tot) | `+0.1336` | `58.33` | **Provisional** |
| **#3** | #3 | `prj_26` | Amber Hours | 2 / 3 (3 tot) | `-0.6682` | `55.56` | **Provisional** |

### Track `trk_06`: Health (3 Projects, 8 Reviews, 1 Usable Judge — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #2 | `prj_09` | Hollow Signal | 1 / 3 (3 tot) | `+1.4142` | `63.89` | **Provisional** |
| **#2** | #1 | `prj_19` | Small Relay | 1 / 3 (2 tot) | `-0.7071` | `66.67` | **Provisional** |
| **#2** | #2 | `prj_17` | Small Loom | 1 / 3 (3 tot) | `-0.7071` | `63.89` | **Provisional** |

### Track `trk_07`: Education (6 Projects, 20 Reviews, 5 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_37` | Salt Loom | 4 / 3 (4 tot) | `+0.5913` | `77.08` | Verified |
| **#2** | #4 | `prj_12` | Open Beacon | 3 / 3 (3 tot) | `+0.0234` | `61.11` | Verified |
| **#3** | #3 | `prj_18` | Open Kiln | 2 / 3 (2 tot) | `+0.0186` | `62.50` | **Provisional** |
| **#4** | #2 | `prj_04` | Green Switch | 3 / 3 (3 tot) | `-0.0321` | `69.44` | Verified |
| **#5** | #5 | `prj_14` | Green Lantern | 5 / 3 (5 tot) | `-0.2776` | `60.00` | Verified |
| **#6** | #6 | `prj_30` | Paper Harbour | 3 / 3 (3 tot) | `-0.3295` | `52.78` | Verified |

### Track `trk_08`: Open hardware (6 Projects, 17 Reviews, 3 Usable Judges — Connected)
| Norm Rank | Raw Rank | Project ID | Title | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :--- | :--- | :---: | :---: | :---: | :--- |
| **#1** | #1 | `prj_34` | Iron Switch | 3 / 3 (3 tot) | `+1.1726` | `83.33` | Verified |
| **#2** | #2 | `prj_10` | Still Beacon | 2 / 3 (2 tot) | `+0.6030` | `79.17` | **Provisional** |
| **#3** | #3 | `prj_36` | Salt Drift | 3 / 3 (3 tot) | `+0.0637` | `66.67` | Verified |
| **#4** | #4 | `prj_27` | Flat Thread | 3 / 3 (3 tot) | `+0.0054` | `61.11` | Verified |
| **#5** | #5 | `prj_20` | Paper Thread | 3 / 3 (3 tot) | `-0.6059` | `55.56` | Verified |
| **#6** | #6 | `prj_23` | Slow Quarry | 3 / 3 (3 tot) | `-1.0379` | `47.22` | Verified |

---

## 4. Diagnostic Cross-Track Pooling Cohort (`all:rb_evt_01_v1`)

When diagnostic cross-track pooling (`track_id="all"`) is selected on `evt_01`:
- **Usable Judges**: 21 of 30 judges meet $N_j \ge 3$ and $\sigma_j > 10^{-6}$ across the pooled event.
- **Excluded Judges (9 total)**:
  - 8 judges have $N_j < 3$ (`jdg_01` Tomas Varga, `jdg_03` Priya Nair, `jdg_05` Kofi Mensah, `jdg_12` Dilan Yilmaz, `jdg_17` Bruno Costa, `jdg_23` Anya Sokolova, `jdg_27` Leila Nasser, `jdg_28` Pavel Ivanov).
  - 1 judge (`jdg_07`, Iva Petrova) has $N_j = 3$ with identical raw scores (`66.67`, `66.67`, `66.67`), giving $\sigma_j = 0.0 \le 10^{-6}$ (`near_zero_variance`).
- **Disconnected Components**:
  - **Component 1**: 20 projects across `trk_02, trk_04, trk_06, trk_08` evaluated by 10 usable judges.
  - **Component 2**: 21 projects across `trk_01, trk_03, trk_05, trk_07` evaluated by 11 usable judges.
  - Because `is_connected = False`, `normalized_rank` is `None` (withheld) for all 41 projects, and only component-local ranks (`Component 1 #1..#20` and `Component 2 #1..#21`) are reported.

### Component 1 Local Rankings (`all:rb_evt_01_v1`, Top 10 of 20 Projects)
| Component Rank | Global Norm Rank | Raw Rank (Event) | Project ID | Title | Track | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :---: | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `Component 1 #1` | — (Withheld) | #16 | `prj_09` | Hollow Signal | `trk_06` | 1 / 3 | `+1.2395` | `63.89` | **Provisional** |
| `Component 1 #2` | — (Withheld) | #2 | `prj_34` | Iron Switch | `trk_08` | 3 / 3 | `+1.2318` | `83.33` | Verified |
| `Component 1 #3` | — (Withheld) | #1 | `prj_11` | Salt Ledger | `trk_02` | 4 / 3 | `+0.8670` | `83.33` | Verified |
| `Component 1 #4` | — (Withheld) | #6 | `prj_16` | Salt Kiln | `trk_04` | 3 / 3 | `+0.6884` | `75.00` | Verified |
| `Component 1 #5` | — (Withheld) | #4 | `prj_25` | Dry Relay | `trk_02` | 3 / 3 | `+0.6095` | `77.78` | Verified |
| `Component 1 #6` | — (Withheld) | #3 | `prj_10` | Still Beacon | `trk_08` | 2 / 3 | `+0.5951` | `79.17` | **Provisional** |
| `Component 1 #7` | — (Withheld) | #8 | `prj_21` | Copper Kiln | `trk_02` | 3 / 3 | `+0.2295` | `72.22` | Verified |
| `Component 1 #8` | — (Withheld) | #10 | `prj_08` | North Drift | `trk_04` | 4 / 3 | `+0.1954` | `70.00` | Verified |
| `Component 1 #9` | — (Withheld) | #13 | `prj_15` | Copper Orbit | `trk_04` | 2 / 3 | `+0.1336` | `66.67` | **Provisional** |
| `Component 1 #10` | — (Withheld) | #13 | `prj_36` | Salt Drift | `trk_08` | 3 / 3 | `+0.0678` | `66.67` | Verified |

### Component 2 Local Rankings (`all:rb_evt_01_v1`, Top 10 of 21 Projects)
| Component Rank | Global Norm Rank | Raw Rank (Event) | Project ID | Title | Track | Usable / Target | Normalized Index | Raw Mean | Status |
| :---: | :---: | :---: | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| `Component 2 #1` | — (Withheld) | #6 | `prj_33` | Slow Trail | `trk_01` | 3 / 3 | `+0.9318` | `75.00` | Verified |
| `Component 2 #2` | — (Withheld) | #5 | `prj_37` | Salt Loom | `trk_07` | 4 / 3 | `+0.7678` | `77.08` | Verified |
| `Component 2 #3` | — (Withheld) | #19 | `prj_02` | Small Meadow | `trk_03` | 2 / 3 | `+0.3273` | `63.89` | **Provisional** |
| `Component 2 #4` | — (Withheld) | #21 | `prj_39` | Paper Anchor | `trk_03` | 2 / 3 | `+0.3070` | `62.50` | **Provisional** |
| `Component 2 #5` | — (Withheld) | #9 | `prj_41` | Dry Harbour | `trk_03` | 4 / 3 | `+0.3029` | `70.83` | Verified |
| `Component 2 #6` | — (Withheld) | #23 | `prj_35` | Warm Beacon | `trk_05` | 2 / 3 | `+0.2347` | `61.67` | **Provisional** |
| `Component 2 #7` | — (Withheld) | #11 | `prj_04` | Green Switch | `trk_07` | 3 / 3 | `+0.1786` | `69.44` | Verified |
| `Component 2 #8` | — (Withheld) | #33 | `prj_07` | Dry Harbour | `trk_03` | 3 / 3 | `+0.1258` | `58.33` | Verified |
| `Component 2 #9` | — (Withheld) | #16 | `prj_31` | Salt Ferry | `trk_01` | 3 / 3 | `+0.1121` | `63.89` | Verified |
| `Component 2 #10` | — (Withheld) | #26 | `prj_12` | Open Beacon | `trk_07` | 3 / 3 | `+0.0560` | `61.11` | Verified |

---

## 5. Objective Interpretation of Rank Changes

Comparing raw score rankings (`raw_rank`) against Z-score normalized rankings (`normalized_rank` within connected tracks, or `component_normalized_rank` within diagnostic components) illustrates two mathematical effects of within-judge standardization:
1. **Scaling by Within-Judge Mean ($\mu_j$) and Dispersion ($\sigma_j$)**:
   - In Track `trk_07` (`Education`), `prj_12` ("Open Beacon", `raw_mean = 61.11`, `raw_rank = #4`) and `prj_04` ("Green Switch", `raw_mean = 69.44`, `raw_rank = #2`) both have 3 usable normalized evaluations, but `prj_12` has `normalized_index = +0.0234` (`normalized_rank = #2`) whereas `prj_04` has `normalized_index = -0.0321` (`normalized_rank = #4`). Mathematically, the judges assigned to `prj_12` had lower cohort means $\mu_j$ than the judges assigned to `prj_04`, so a raw score of `61.11` represented a positive deviation above those judges' means, whereas `69.44` fell slightly below the assigned judges' means for `prj_04`.
2. **Effect of Usable-Sample Filtering ($N_j < 3$ and $\sigma_j \le \epsilon$)**:
   - When a project is evaluated by one or more judges who are excluded from normalization (due to $N_j < 3$ within the cohort or $\sigma_j = 0$), `raw_mean` averages over all completed reviews while `normalized_index` averages only over the remaining usable judges. For example, in Track `trk_04` (`Security`), `prj_08` ("North Drift") has 5 completed reviews (`raw_mean = 70.00`, `raw_rank = #2`) of which 3 come from usable judges within `trk_04` (`normalized_index = -0.0913`, `normalized_rank = #3`), whereas `prj_15` ("Copper Orbit", `raw_mean = 66.67`, `raw_rank = #3`) has 2 usable reviews (`normalized_index = +0.1336`, `normalized_rank = #2`, flagged **Provisional**).
3. **Interpretive Limitations**:
   - A rank shift between raw and normalized scores is a mathematical consequence of standardizing each judge's sample distribution ($N_j, \mu_j, \sigma_j$) and filtering unusable judges. With small per-judge sample sizes ($N_j \in [3, 6]$) and non-random project-to-judge assignments, a judge's observed sample mean $\mu_j$ and dispersion $\sigma_j$ reflect both the judge's scoring scale and the specific subset of projects assigned to that judge. Therefore, rank differences should be interpreted as calibrated relative indicators rather than standalone proof of improved fairness or pure judge generosity.
