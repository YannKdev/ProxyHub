import socket
import threading
import time

import pytest
import uvicorn
from app.main import create_app
from fakes.fake_gluetun import FakeGluetun
from proxyhub_client import ProxyHub, ProxyHubError


def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.01)
    return server, thread, port


@pytest.fixture
def fake():
    return FakeGluetun(["1.1.1.1", "2.2.2.2", "3.3.3.3"])


@pytest.fixture
def hub_app(settings, new_exit, fake):
    return create_app(settings, {"vpn-1": new_exit(fake, ready_timeout_seconds=0.3)})


@pytest.fixture
def hub(hub_app, api_token):
    server, thread, port = serve(hub_app)
    yield ProxyHub(f"http://127.0.0.1:{port}", api_token, "user", "p@ss:word")
    server.should_exit = True
    thread.join(5)


def test_exits_and_ip(hub):
    assert [e["id"] for e in hub.exits()] == ["vpn-1"]
    assert hub.ip("vpn-1") == "1.1.1.1"


def test_rotate_waits_for_result(hub):
    result = hub.rotate("vpn-1")
    assert result["changed"] is True
    assert result["new_ip"] == "2.2.2.2"


def test_rotate_without_waiting(hub):
    operation_id = hub.rotate("vpn-1", wait=False)
    assert hub.wait(operation_id, poll=0.05)["new_ip"] == "2.2.2.2"


def test_session_sets_proxy_and_closes(hub):
    with hub.session("vpn-1") as s:
        assert s.proxies["https"] == "http://user:p%40ss%3Aword@127.0.0.1:8081"
        assert s.proxyhub["ip"] == "1.1.1.1"
        assert hub.exits()[0]["active_sessions"] == 1
    assert hub.exits()[0]["active_sessions"] == 0


def test_session_closes_on_error(hub):
    with pytest.raises(RuntimeError), hub.session("vpn-1"):
        raise RuntimeError("scraping failed")
    assert hub.exits()[0]["active_sessions"] == 0


def test_session_switches_country(hub, fake):
    with hub.session("vpn-1", country="Belgium") as s:
        assert s.proxyhub["country"] == "Belgium"
    assert fake.countries == ["Belgium"]


def test_session_waits_for_running_rotation(hub, fake):
    fake._ips = []  # IP inchangée : la rotation fait tous ses essais, donc dure un peu
    operation_id = hub.rotate("vpn-1", wait=False)
    with hub.session("vpn-1") as s:
        assert hub.operation(operation_id)["status"] == "done"
        assert s.proxyhub["session_id"]


def test_errors(hub, api_token):
    with pytest.raises(ProxyHubError) as error:
        hub.ip("nope")
    assert error.value.status == 404

    bad = ProxyHub(hub.base_url, "wrong", "user", "password")
    with pytest.raises(ProxyHubError) as error:
        bad.exits()
    assert error.value.status == 401
