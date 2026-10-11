import pytest
from app.guard import Guard
from app.main import create_app
from fakes.fake_gluetun import FakeGluetun
from fastapi.testclient import TestClient


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_guard(clock):
    return Guard(max_failures=2, failure_window_seconds=600, block_seconds=1800, clock=clock)


def test_block_after_failures():
    clock = Clock()
    guard = make_guard(clock)
    guard.auth_failed("1.1.1.1")
    assert guard.blocked_for("1.1.1.1") is None
    guard.auth_failed("1.1.1.1")
    assert guard.blocked_for("1.1.1.1") == 1800
    assert guard.blocked_for("2.2.2.2") is None
    clock.now += 1801
    assert guard.blocked_for("1.1.1.1") is None


def test_old_failures_are_forgotten():
    clock = Clock()
    guard = make_guard(clock)
    guard.auth_failed("1.1.1.1")
    clock.now += 601
    guard.auth_failed("1.1.1.1")
    assert guard.blocked_for("1.1.1.1") is None
    assert len(guard._failures["1.1.1.1"]) == 1


@pytest.fixture
def client(settings, new_exit):
    settings = settings.model_copy(update={"auth_max_failures": 3})
    app = create_app(settings, {"vpn-1": new_exit(FakeGluetun(["1.1.1.1"]))})
    with TestClient(app) as client:
        yield client


def test_api_blocks_after_bad_tokens(client, auth):
    bad = {"Authorization": "Bearer wrong"}
    assert [client.get("/exits", headers=bad).status_code for _ in range(3)] == [401] * 3
    response = client.get("/exits", headers=auth)
    assert response.status_code == 429
    assert response.json()["detail"] == "too many failed authentications"
    assert int(response.headers["Retry-After"]) > 0
    assert client.get("/health").status_code == 200


def test_api_has_no_rate_limit(client, auth):
    assert all(client.get("/exits", headers=auth).status_code == 200 for _ in range(200))
