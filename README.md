# HackForge

**Build. Submit. Judge.** A self-hosted hackathon submission and judging platform.

## Current milestone: foundation

FastAPI, PostgreSQL, Alembic migrations, repeatable official fixture import, public gallery with search/track filtering, project details, and a database health check. All styling is served locally.

**No tiers are claimed yet.** Authentication, editable submissions, deadline enforcement, organizer tools, and judging workflows are next. The official checker is included unmodified; an acceptance report will be generated when those workflows exist. This is not a finished submission.

## Run

```sh
docker compose up --build
```

Open http://localhost:8080. Subsequent starts use `docker compose up`.

The first build/pull requires internet access. Once the images are prepared, startup and gallery operation need no internet. The database lives in a named volume and remains across restarts. `docker compose down -v` deliberately deletes it. The database is not published to the host; the app binds localhost only. Default credentials are for local evaluation.

## Test

```sh
docker compose exec app python -m pytest -q
```

## Official input

`fixtures.json`, `run.py`, and `docs/official-spec.md` were downloaded from https://dogfoodhack.com/spec/ on September 27, 2026. The input contains 41 project records (including the duplicate), 40 teams, 30 judges, 8 tracks, and 126 reviews. The original closed deadline is preserved. Re-import adds missing records and does not replace existing values.

## Next milestones

1. Local accounts, database sessions, event-scoped roles, CSRF protection.
2. Event configuration, invite-based teams, drafts and submissions with server-side deadline checks.
3. Judge invitations/assignments, weighted rubric, review isolation and progress.
4. Documented normalization, CSV exports, audit history, official acceptance report.
5. Offline release verification, final documentation and five-minute demo.
