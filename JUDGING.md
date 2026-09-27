# Judging Engine & Normalization Status

## Current Status: Phase 1 (Foundation)

> **IMPORTANT**: Judging workflows, scoring forms, judge assignments, and score normalization algorithms are **NOT** yet implemented.
>
> In Phase 1, review records from `fixtures.json` are imported into database storage only. All review records, criteria ratings, judge emails, and private comments are strictly isolated on the backend. No public route, gallery view, or API endpoint exposes judging data or produces rankings.

---

## Planned Architecture (Phase 3 & Phase 4)

### 1. Backend-Enforced Judge Isolation (Phase 3)
- **Problem**: In commercial hackathon platforms, peer scores are frequently hidden only in client-side HTML templates while API endpoints leak all evaluations.
- **Solution**: HackForge will enforce judge isolation at the database query layer:
  - When a judge requests evaluations via `GET /api/judge/scores`, the authenticated session context is extracted.
  - The query strictly filters `WHERE review.judge_id = current_user.judge_id`.
  - Any request targeting another judge's evaluations (e.g. `GET /api/judge/scores?judge=judge_a` from `judge_b`) is rejected with **HTTP 403 Forbidden**.
  - Participants attempting to access judge endpoints are rejected with **HTTP 403 Forbidden**.

### 2. Weighted Scoring Rubrics (Phase 3)
- Organizers define criteria with custom scales (e.g., $1 \dots 5$) and positive weights ($w_i$).
- The raw composite score for a submission from judge $j$ is calculated as:
  $$S_{j, p} = \frac{\sum_{i} w_i \cdot s_{j, p, i}}{\sum_{i} w_i}$$
- **Handling Incomplete Batches**: Missing reviews remain missing. Missing evaluations are never imputed as zero, which would catastrophically penalize submissions through no fault of their own.

### 3. Cross-Judge Score Normalization Proof (Phase 4)
Hackathon judges exhibit varying degrees of leniency and strictness. HackForge will implement a documented, mathematically sound normalization algorithm:

#### A. Standard Z-Score Calibration
For judge $j$ with mean evaluation $\mu_j$ and sample standard deviation $\sigma_j$:
$$z_{j, p} = \frac{S_{j, p} - \mu_j}{\sigma_j}$$

The normalized score across all judges evaluating project $p$ is then scaled back to the standard rubric range:
$$\bar{S}_p = \mu_{\text{global}} + \sigma_{\text{global}} \cdot \left( \frac{1}{|J_p|} \sum_{j \in J_p} z_{j, p} \right)$$

#### B. Singular / Zero-Variance Boundary Case ($\sigma_j = 0$)
In real hackathons (and in the official fixture dataset, e.g. `jdg_01` and `jdg_07`), judges sometimes give the exact same rating to every submission:
- When $\sigma_j = 0$, division by zero is undefined.
- **Resolution**: Detect zero variance ($\sigma_j < 10^{-6}$) and apply a mean-shift offset instead of Z-score scaling:
  $$S'_{j, p} = S_{j, p} + (\mu_{\text{global}} - \mu_j)$$
  This centers the judge's evaluations to the global mean without distorting variance.

#### C. Sparse Review Connectivity
With 30 judges and 41 projects, the review graph is sparse ($126 / (30 \times 41) \approx 10.2\%$). The normalization engine will compute global connected components before calibration to ensure statistical comparability.

---

## Organizer Verification & Audits (Phase 4)
- **Progress View**: Tracks completed vs pending evaluations per track.
- **Audit Export**: CSV export available exclusively to authenticated organizers (`GET /api/export.csv`), including raw scores, judge identifiers, normalized scores, and ranking deltas.
