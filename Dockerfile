FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /portal

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --shell /bin/bash portal && \
    mkdir -p /portal && \
    chown -R portal:portal /portal

COPY --chown=portal:portal . .

USER portal
EXPOSE 8080

CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8080"]
