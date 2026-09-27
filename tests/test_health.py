from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_liveness_probe(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "hackforge"}


def test_readiness_probe_healthy(client):
    with patch("app.main.check_database_health", return_value=True):
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "hackforge"
        assert data["database"] == "connected"


def test_readiness_probe_unhealthy(client):
    with patch("app.main.check_database_health", return_value=False):
        response = client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["service"] == "hackforge"
        assert data["error"] == "database unavailable"
        # Must not leak connection string or raw exception
        assert "password" not in response.text
        assert "postgresql://" not in response.text


def test_health_alias(client):
    with patch("app.main.check_database_health", return_value=True):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
