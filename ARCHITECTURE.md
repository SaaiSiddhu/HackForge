# Architecture

Browser → FastAPI/Jinja2 → SQLAlchemy → PostgreSQL. Docker Compose waits for database readiness, runs Alembic migrations, imports fixtures in a transaction, then starts the HTTP server. Local static assets require no CDN. The initial public read routes select project, track and team names only; judge identities and review data are not exposed.

Upcoming authentication and authorization will share service-layer policies across HTML and JSON endpoints. No private endpoints exist in this foundation milestone.
