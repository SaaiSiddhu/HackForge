FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /portal

COPY requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

RUN useradd --create-home --shell /bin/bash portal && \
    mkdir -p /portal/uploads && \
    chown -R portal:portal /portal

COPY --chown=portal:portal . .
RUN mkdir -p /portal/uploads && chown -R portal:portal /portal/uploads

USER portal
EXPOSE 8080

CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8080"]
