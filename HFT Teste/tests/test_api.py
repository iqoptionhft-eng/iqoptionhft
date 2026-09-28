import pytest
from fastapi.testclient import TestClient

from app.api import create_app

TOKEN = "t" * 32


@pytest.fixture
def client(make_engine):
    eng = make_engine()
    app = create_app(eng, TOKEN)
    return TestClient(app, base_url="http://127.0.0.1:8787"), eng


def test_host_estranho_e_recusado(make_engine):
    app = create_app(make_engine(), TOKEN)
    c = TestClient(app, base_url="http://evil.example.com")
    assert c.get("/api/state", headers={"X-Token": TOKEN}).status_code == 400
    assert c.get("/").status_code == 400


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/state"), ("post", "/api/start"), ("post", "/api/connect"), ("post", "/api/stop"),
     ("post", "/api/settings"), ("post", "/api/account"), ("get", "/api/health"), ("post", "/api/backtest")],
)
def test_endpoints_exigem_token(client, method, path):
    c, _ = client
    assert getattr(c, method)(path).status_code == 401
    assert getattr(c, method)(path, headers={"X-Token": "errado" * 6}).status_code == 401


def test_start_sem_token_nao_liga_motor(client):
    c, eng = client
    c.post("/api/start")
    assert eng._thread is None


def test_state_com_token_sem_email(client):
    c, _ = client
    r = c.get("/api/state", headers={"X-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["account"] == "PRACTICE"
    assert "x@y.z" not in r.text


def test_settings_recusa_account(client):
    c, eng = client
    r = c.post("/api/settings", json={"account": "REAL"}, headers={"X-Token": TOKEN})
    assert r.status_code == 422
    assert eng.account == "PRACTICE"


@pytest.mark.parametrize("d,code", [(1, 200), (5, 200), (2, 422), (3, 422)])
def test_settings_duration(client, d, code):
    c, _ = client
    r = c.post("/api/settings", json={"duration_min": d}, headers={"X-Token": TOKEN})
    assert r.status_code == code


def test_account_real_recusada_sem_allow_real(client):
    c, eng = client
    r = c.post("/api/account", json={"mode": "REAL", "confirm": "REAL"}, headers={"X-Token": TOKEN})
    assert r.status_code == 403
    assert eng.account == "PRACTICE"


def test_index_publico_mas_sem_token(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert TOKEN not in r.text
