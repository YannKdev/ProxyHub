import asyncio
import socket
import threading
import time

import pytest
import requests
import uvicorn
from app.main import create_app
from fakes.fake_gluetun import FakeGluetun
from fakes.fake_upstream import FakeUpstream
from proxyhub_client import ProxyHub, ProxyHubError


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(app):
    port = free_port()
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
def upstream():
    """Faux proxy de sortie, dans sa propre boucle asyncio."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    fake_upstream = FakeUpstream("vpn-1", password="p@ss:word")
    asyncio.run_coroutine_threadsafe(fake_upstream.start(), loop).result(5)
    yield fake_upstream
    asyncio.run_coroutine_threadsafe(fake_upstream.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


@pytest.fixture
def running(settings, new_exit, fake, upstream, api_token):
    proxy_port = free_port()
    app = create_app(
        settings.model_copy(update={"proxy_port": proxy_port, "proxy_password": "p@ss:word"}),
        {"vpn-1": new_exit(fake, ready_timeout_seconds=0.3, proxy_url=upstream.url)},
    )
    server, thread, port = serve(app)
    hub = ProxyHub(
        f"http://127.0.0.1:{port}",
        api_token,
        proxy_user="user",
        proxy_password="p@ss:word",
        proxy_port=proxy_port,
    )
    yield hub
    server.should_exit = True
    thread.join(5)


def test_proxy_url_options():
    hub = ProxyHub("https://api.example.org", proxy_user="user", proxy_password="p@ss:word")
    assert hub.proxy_url() == "http://user:p%40ss%3Aword@api.example.org:8080"
    assert hub.proxy_url(session="a1", pool="cyberghost", country="BE", hold=30) == (
        "http://user-pool-cyberghost-country-be-session-a1-hold-30:p%40ss%3Aword"
        "@api.example.org:8080"
    )
    assert hub.proxy_url(session=7).startswith("http://user-session-7:")


@pytest.mark.parametrize("session", ["a-b", "", "x" * 65, "é"])
def test_bad_session_names(session):
    hub = ProxyHub("https://api.example.org", proxy_user="user", proxy_password="pw")
    with pytest.raises(ValueError):
        hub.proxy_url(session=session)


def test_proxy_needs_credentials():
    with pytest.raises(ValueError):
        ProxyHub("https://api.example.org", token="t").proxy_url()


def test_proxy_session_is_configured():
    hub = ProxyHub("https://api.example.org", proxy_user="user", proxy_password="pw")
    with hub.proxy(session="a") as s:
        assert s.trust_env is False
        assert s.proxies["https"] == s.proxies["http"] == hub.proxy_url(session="a")


def test_requests_through_the_entry_proxy(running):
    with running.proxy(session="job1") as s:
        response = s.get("http://example.com/page", timeout=10)
    assert response.status_code == 200
    assert response.text.startswith("exit=vpn-1\nGET http://example.com/page HTTP/1.1")
    [session] = running.sessions()
    assert session["label"] == "job1"
    running.release(session["id"])
    assert running.sessions() == []


def test_exits_and_ip(running):
    [info] = running.exits()
    assert (info["id"], info["pool"]) == ("vpn-1", "nordvpn")
    assert running.ip("vpn-1") == "1.1.1.1"


def test_rotate_waits_for_result(running):
    result = running.rotate("vpn-1")
    assert result["changed"] is True
    assert result["new_ip"] == "2.2.2.2"


def test_rotate_without_waiting(running):
    operation_id = running.rotate("vpn-1", wait=False)
    assert running.wait(operation_id, poll=0.05)["new_ip"] == "2.2.2.2"


def test_rotate_waits_for_cooldown(settings, new_exit, api_token):
    app = create_app(settings, {"vpn-1": new_exit(FakeGluetun(["1.1.1.1"]), cooldown_seconds=1)})
    server, thread, port = serve(app)
    try:
        hub = ProxyHub(f"http://127.0.0.1:{port}", api_token)
        hub.wait(hub.rotate("vpn-1", wait=False), poll=0.05)
        with pytest.raises(ProxyHubError) as error:
            hub.rotate("vpn-1", wait=False)
        assert error.value.status == 429
        assert error.value.data["retry_after"] == 1
        assert hub.rotate("vpn-1")["attempts"] == 3
    finally:
        server.should_exit = True
        thread.join(5)


def test_errors(running):
    with pytest.raises(ProxyHubError) as error:
        running.ip("nope")
    assert error.value.status == 404
    with pytest.raises(ProxyHubError) as error:
        running.release("nope")
    assert error.value.status == 404

    with pytest.raises(ProxyHubError) as error:
        ProxyHub(running.base_url, "wrong").exits()
    assert error.value.status == 401


def test_wrong_proxy_password_is_refused(running):
    bad = ProxyHub(running.base_url, proxy_user="user", proxy_password="nope")
    bad.proxy_port = running.proxy_port
    with bad.proxy() as s, pytest.raises(requests.exceptions.ProxyError):
        s.get("https://example.com/", timeout=10)
