# HackForge Judging Engine: Score Normalization & Fixture Analysis

## 1. Mathematical Framework & Calibration Engine

In hackathon evaluations, different judges exhibit systematic scoring biases:
- **Lenient judges** award high scores with low discrimination.
- **Harsh judges** award low scores across the board.
- **Variable judges** span the full rubric range.

A simple raw average unfairly penalizes projects evaluated by harsh judges and inflates projects evaluated by lenient judges. HackForge implements a **defensible judge-wise Z-score normalization baseline** with guardrails against sparse sampling and near-zero variance.

---

### A. Raw Review Score Recalculation
Raw review scores are recalculated from granular criterion records and active rubric weights using full floating-point precision:

$$\text{Raw Score} = 100 \times \frac{\sum_{i=1}^C w_i \cdot \frac{s_i - \min_i}{\max_i - \min_i}}{\sum_{i=1}^C w_i}$$

where:
- $C$ is the number of criteria in the bound rubric.
- $w_i > 0$ is the weight of criterion $i$.
- $s_i \in [\min_i, \max_i]$ is the assigned score.
- The stored `review.raw_score` is never trusted blindly; it is validated and recomputed from source criterion rows.

---

### B. Judge Cohort Statistics & Population Standard Deviation
For each judge $j$ in cohort $C$, let $S_j = [S_{j,1}, S_{j,2}, \dots, S_{j, N_j}]$ denote the set of eligible recalculated raw scores:

$$\mu_j = \frac{1}{N_j} \sum_{k=1}^{N_j} S_{j,k}$$

$$\sigma_j = \sqrt{ \frac{1}{N_j} \sum_{k=1}^{N_j} (S_{j,k} - \mu_j)^2 }$$

> **Population Standard Deviation**: In accordance with the specification, $\sigma_j$ is the **population** standard deviation (dividing by $N_j$, not $N_j - 1$), matching Python's `statistics.pstdev()`.

---

### C. Guardrails Against Sparse Samples & Zero Variance
A judge is marked **usable** for normalization if and only if both conditions hold:
1. **Minimum Sample Size**: $N_j \ge N_{\min}$ (default $N_{\min} = 3$). Judges with $N_j < 3$ lack sufficient observations to establish a statistically meaningful mean and dispersion.
2. **Variance Threshold**: $\sigma_j > \epsilon$ (default $\epsilon = 1\text{e-}6$). Judges who give identical scores to every project produce zero standard deviation ($\sigma_j = 0$), where division by zero would occur.

**Treatment of Unusable Judges**:
- Raw scores from unusable judges are **retained** in project raw averages and CSV review audits.
- Normalized contributions $z_{j,k}$ are set to `None` with an explicit reason (`"insufficient_samples"` or `"near_zero_variance"`).
- Projects relying on unusable judges are flagged with `is_provisional = True`.

---

### D. Z-Score Calculation & Project Normalized Index
For each usable judge $j$, the standardized score for project $k$ is:

$$z_{j,k} = \frac{S_{j,k} - \mu_j}{\sigma_j}$$

For project $k$, let $Z_k$ be the collection of usable normalized scores:

$$\text{Normalized Index}_k = \frac{1}{|Z_k|} \sum_{z \in Z_k} z \quad (\text{if } |Z_k| > 0, \text{ else } \text{None})$$

- If $|Z_k| < T$ (where $T = \text{event.target\_reviews\_per\_project}$), the project is flagged as **Provisional**.
- If $|Z_k| = 0$, the project normalized index is left blank (`None` / empty string in CSV), **never fabricated as zero**.

---

### E. Graph Connectivity & Disconnected Components Warning
The bipartite evaluation graph $G = (V_{\text{projects}} \cup V_{\text{usable\_judges}}, E)$ connects judge $j$ and project $k$ if $j$ evaluated $k$.
- If $G$ contains $> 1$ connected components, projects in different components were evaluated by disjoint sets of judges.
- Relative Z-scores between disconnected components cannot be directly calibrated against each other.
- HackForge detects components via breadth-first search (BFS) and surfaces a **prominent warning** on the organizer dashboard and API.

---

## 2. Empirical Analysis on Fixture Dataset (`evt_01`)

The official HackForge dataset (`evt_01`) consists of:
- **41 Submitted Projects**
- **30 Evaluated Judges**
- **126 Finalized Reviews** across 8 Tracks
- **1 Evaluation Rubric** (`rb_evt_01_v1`, 3 criteria, equal weights)
- **Target Coverage**: 3 reviews per project

### A. Judge Calibration & Usability Breakdown

| Judge ID | Evaluations ($N_j$) | Mean Score ($\mu_j$) | Std Dev ($\sigma_j$) | Usability Status | Exclusion Reason |
| :--- | :---: | :---: | :---: | :--- | :--- |
| `jdg_01` | 1 | 25.00 | 0.0000 | **Excluded** | `insufficient_samples (n=1 < 3)` |
| `jdg_02` | 6 | 80.56 | 18.4257 | **Usable** | — |
| `jdg_03` | 2 | 70.83 | 12.5000 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_04` | 4 | 64.58 | 16.0024 | **Usable** | — |
| `jdg_05` | 2 | 62.50 | 4.1667 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_06` | 3 | 61.11 | 10.3935 | **Usable** | — |
| `jdg_07` | 3 | 75.00 | 0.0000 | **Excluded** | `near_zero_variance (pstdev=0.000000e+00 <= 1.000000e-06)` |
| `jdg_08` | 3 | 61.11 | 17.1234 | **Usable** | — |
| `jdg_09` | 5 | 61.67 | 12.4722 | **Usable** | — |
| `jdg_10` | 3 | 55.56 | 10.3935 | **Usable** | — |
| `jdg_11` | 6 | 65.28 | 17.6230 | **Usable** | — |
| `jdg_12` | 2 | 62.50 | 12.5000 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_13` | 3 | 75.00 | 13.6083 | **Usable** | — |
| `jdg_14` | 3 | 50.00 | 13.6083 | **Usable** | — |
| `jdg_15` | 6 | 76.39 | 18.8909 | **Usable** | — |
| `jdg_16` | 6 | 65.28 | 17.6230 | **Usable** | — |
| `jdg_17` | 2 | 62.50 | 4.1667 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_18` | 3 | 61.11 | 3.9284 | **Usable** | — |
| `jdg_19` | 4 | 58.33 | 14.4338 | **Usable** | — |
| `jdg_20` | 6 | 52.78 | 18.4257 | **Usable** | — |
| `jdg_21` | 4 | 66.67 | 17.6777 | **Usable** | — |
| `jdg_22` | 5 | 68.33 | 16.9967 | **Usable** | — |
| `jdg_23` | 1 | 58.33 | 0.0000 | **Excluded** | `insufficient_samples (n=1 < 3)` |
| `jdg_24` | 11 | 59.09 | 14.8454 | **Usable** | — |
| `jdg_25` | 5 | 61.67 | 10.0000 | **Usable** | — |
| `jdg_26` | 10 | 67.50 | 11.4564 | **Usable** | — |
| `jdg_27` | 2 | 50.00 | 8.3333 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_28` | 2 | 54.17 | 4.1667 | **Excluded** | `insufficient_samples (n=2 < 3)` |
| `jdg_29` | 9 | 62.96 | 9.7112 | **Usable** | — |
| `jdg_30` | 4 | 77.08 | 12.3252 | **Usable** | — |

**Key Calibration Findings**:
- **21 Usable Judges** (70%): Contributed to standardized Z-scores.
- **8 Judges with Insufficient Samples** (`jdg_01`, `jdg_03`, `jdg_05`, `jdg_12`, `jdg_17`, `jdg_23`, `jdg_27`, `jdg_28`): Evaluated only 1 or 2 projects. Correctly excluded from normalization to prevent high-leverage distortion.
- **1 Judge with Zero Variance** (`jdg_07`): Evaluated 3 projects but gave each the identical score of 75.0 ($\sigma = 0.0000$). Correctly identified and excluded to avoid division by zero.

---

### B. Disconnected Components in the Evaluation Network

When evaluating the bipartite graph across all 41 projects and 21 usable judges, the evaluation network is partitioned into **2 disconnected components**:

- **Component 1**: 20 projects (`prj_01`–`prj_20`), 10 usable judges (`jdg_02`, `jdg_04`, `jdg_06`, `jdg_08`, `jdg_09`, `jdg_10`, `jdg_11`, `jdg_13`, `jdg_14`, `jdg_15`).
- **Component 2**: 21 projects (`prj_21`–`prj_41`), 11 usable judges (`jdg_16`, `jdg_18`, `jdg_19`, `jdg_20`, `jdg_21`, `jdg_22`, `jdg_24`, `jdg_25`, `jdg_26`, `jdg_29`, `jdg_30`).

No judge in Component 1 evaluated any project in Component 2. The organizer console and API prominently display this warning:
> *⚠️ Disconnected Evaluation Network Detected: Evaluation network is partitioned into 2 disconnected components among usable judges and projects. Cross-component normalized z-scores cannot be directly calibrated against each other.*

---

### C. Rankings Comparison (Top 10 Projects)

| Norm Rank | Raw Rank | Project ID | Normalized Index | Raw Mean | Usable / Target | Provisional? | Track ID |
| :---: | :---: | :--- | :---: | :---: | :---: | :---: | :--- |
| **#1** | #16 | `prj_09` | **+1.2395** | 63.89 | 2 / 3 | **Provisional** | `trk_06` |
| **#2** | #2 | `prj_34` | **+1.2318** | 83.33 | 3 / 3 | Verified | `trk_08` |
| **#3** | #6 | `prj_33` | **+0.9318** | 75.00 | 3 / 3 | Verified | `trk_01` |
| **#4** | #1 | `prj_11` | **+0.8670** | 83.33 | 3 / 3 | Verified | `trk_02` |
| **#5** | #5 | `prj_37` | **+0.7678** | 77.08 | 3 / 3 | Verified | `trk_07` |
| **#6** | #6 | `prj_16` | **+0.6884** | 75.00 | 2 / 3 | **Provisional** | `trk_04` |
| **#7** | #4 | `prj_25` | **+0.6095** | 77.78 | 3 / 3 | Verified | `trk_02` |
| **#8** | #3 | `prj_10` | **+0.5951** | 79.17 | 2 / 3 | **Provisional** | `trk_08` |
| **#9** | #19 | `prj_02` | **+0.3273** | 63.89 | 2 / 3 | **Provisional** | `trk_03` |
| **#10** | #21 | `prj_39` | **+0.3070** | 62.50 | 2 / 3 | **Provisional** | `trk_03` |

---

### D. Notable Ranking Shifts (Raw vs Normalized)

1. **`prj_19` (Dropped 17 spots: Raw #13 $\to$ Norm #30)**:
   - Evaluated by judges with above-average mean scores. While its raw score was 66.67, its normalized index is $-0.4767$, revealing that relative to its judges' typical grading scale, it scored below their average.
2. **`prj_07` (Improved 16 spots: Raw #33 $\to$ Norm #17)**:
   - Evaluated by exceptionally tough judges. Its modest raw mean of 58.33 translates to a positive normalized index of $+0.1258$, properly recognizing performance evaluated under harsher scrutiny.
3. **`prj_01` (Dropped 16 spots: Raw #24 $\to$ Norm #40)**:
   - Evaluated by `jdg_01` (excluded, $N=1$), `jdg_02` (lenient judge, $\mu=80.56$), and `jdg_03` (excluded, $N=2$). Its single usable evaluation from lenient `jdg_02` yielded a normalized score of $-1.1355$.
4. **`prj_09` (Improved 15 spots: Raw #16 $\to$ Norm #1, Provisional)**:
   - Received high scores from relatively tough judges, but only has 2 usable evaluations ($< 3$). The engine transparently marks it as **Provisional**, preventing unverified top ranking without sufficient coverage.
