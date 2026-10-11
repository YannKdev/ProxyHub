import time

import pytest
from app.main import create_app
from app.store import utcnow
from fakes.fake_gluetun import FakeGluetun
from fastapi.testclient import TestClient


@pytest.fixture
def fake():
    return FakeGluetun(["1.1.1.1", "2.2.2.2"])


@pytest.fixture
def client(settings, fake, new_exit):
    app = create_app(settings, {"vpn-1": new_exit(fake, cooldown_seconds=60)})
    with TestClient(app) as client:
        yield client


def wait_for(client, auth, operation_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        op = client.get(f"/operations/{operation_id}", headers=auth).json()
        if op["status"] != "running":
            return op
        time.sleep(0.01)
    raise AssertionError("operation still running")


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X-Token": "x"}])
def test_token_required(client, headers):
    assert client.get("/exits", headers=headers).status_code == 401


def test_list_exits(client, auth):
    exits = client.get("/exits", headers=auth).json()
    assert [e["id"] for e in exits] == ["vpn-1"]
    assert exits[0]["proxy_port"] == 8081


def test_unknown_exit(client, auth):
    assert client.get("/exits/nope/ip", headers=auth).status_code == 404
    assert client.post("/exits/nope/rotate", headers=auth).status_code == 404
    assert client.get("/exits/nope/history", headers=auth).status_code == 404


def test_measure_ip(client, auth, fake):
    assert client.get("/exits/vpn-1/ip", headers=auth).json() == {"exit": "vpn-1", "ip": "1.1.1.1"}
    fake.reachable = False
    assert client.get("/exits/vpn-1/ip", headers=auth).status_code == 503


def test_rotation_flow(client, auth):
    response = client.post("/exits/vpn-1/rotate?country=Belgium", headers=auth)
    assert response.status_code == 202

    op = wait_for(client, auth, response.json()["operation_id"])
    assert op["status"] == "done"
    assert op["result"]["new_ip"] == "2.2.2.2"
    assert op["params"] == {"country": "Belgium", "trigger": "api"}

    vpn = client.get("/exits", headers=auth).json()[0]
    assert vpn["ip"] == "2.2.2.2"
    assert vpn["country"] == "Belgium"
    assert vpn["last_rotation_at"] is not None

    history = client.get("/exits/vpn-1/history", headers=auth).json()
    assert any(entry["event"] == "rotation" for entry in history)


def test_busy_exit_returns_409(client, auth):
    client.app.state.hub.running["vpn-1"] = "other-operation"
    assert client.post("/exits/vpn-1/rotate", headers=auth).status_code == 409


def test_cooldown_returns_429(client, auth):
    client.app.state.hub.store.save_rotation_time("vpn-1", utcnow())
    response = client.post("/exits/vpn-1/rotate", headers=auth)
    assert response.status_code == 429
    assert 0 < int(response.headers["Retry-After"]) <= 60


@pytest.mark.parametrize("country", ["", "<script>", "a" * 60, "1234"])
def test_invalid_country_is_rejected(client, auth, country):
    response = client.post("/exits/vpn-1/rotate", params={"country": country}, headers=auth)
    assert response.status_code == 422


def test_unknown_operation(client, auth):
    assert client.get("/operations/nope", headers=auth).status_code == 404


def test_session_lifecycle(client, auth):
    response = client.post("/exits/vpn-1/sessions", headers=auth)
    assert response.status_code == 201
    session = response.json()
    assert session["proxy_url"] == "http://testserver:8081"
    assert session["expires_at"]
    assert client.get("/exits", headers=auth).json()[0]["active_sessions"] == 1

    assert client.delete(f"/sessions/{session['session_id']}", headers=auth).status_code == 204
    vpn = client.get("/exits", headers=auth).json()[0]
    assert vpn["active_sessions"] == 0
    assert vpn["sessions_since_rotation"] == 1


def test_close_unknown_session(client, auth):
    assert client.delete("/sessions/nope", headers=auth).status_code == 404


def test_rotation_with_active_session_needs_force(client, auth):
    client.post("/exits/vpn-1/sessions", headers=auth)
    response = client.post("/exits/vpn-1/rotate", headers=auth)
    assert response.status_code == 409
    assert response.json()["active_sessions"] == 1

    response = client.post("/exits/vpn-1/rotate?force=true", headers=auth)
    assert response.status_code == 202
    wait_for(client, auth, response.json()["operation_id"])


def test_session_during_rotation_returns_operation(client, auth):
    client.app.state.hub.running["vpn-1"] = "other-operation"
    response = client.post("/exits/vpn-1/sessions", headers=auth)
    assert response.status_code == 409
    assert response.json()["operation_id"] == "other-operation"


def test_session_in_other_country(client, auth):
    response = client.post("/exits/vpn-1/sessions?country=Belgium", headers=auth)
    assert response.status_code == 409
    assert response.json()["country"] == "france"


def test_session_on_down_exit(client, auth, fake):
    fake.reachable = False
    client.get("/exits/vpn-1/ip", headers=auth)
    assert client.post("/exits/vpn-1/sessions", headers=auth).status_code == 503
