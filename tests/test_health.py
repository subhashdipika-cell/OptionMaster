from fastapi.testclient import TestClient

from optionmaster.main import app


def test_health_defaults_to_paper_mode(monkeypatch):
    monkeypatch.delenv("OPTIONMASTER_EXECUTION_MODE", raising=False)
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["execution_mode"] == "PAPER"
