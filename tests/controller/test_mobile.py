import asyncio
import base64
import textwrap

import pytest
from app.config import ConfigError, MobileExitConfig, VpnExitConfig, load_exits
from app.guard import Guard
from app.hub import Hub, NoFreeExit, SessionFailed
from app.main import create_app
from app.mobile import MobileExit
from app.proxy import EntryProxy
from app.store import Store
from fakes.fake_agent import FakeAgent
from fakes.fake_gluetun import FakeGluetun
from fakes.fake_upstream import FakeUpstream
from fastapi.testclient import TestClient

PHONE_IPS = ["198.51.100.1", "198.51.100.2", "198.51.100.3"]


def mobile_exit(agent: FakeAgent, **overrides) -> MobileExit:
    options = {
        "id": "mobile-1",
        "type": "mobile",
        "proxy_url": "http://tunnel:13128",
        "agent_url": "http://tunnel:19000",
        "cooldown_seconds": 0,
        "ready_timeout_seconds": 0.05,
        "poll_seconds": 0.01,
    }
    agent_client, web = agent.clients()
    config = MobileExitConfig(**(options | overrides))
    return MobileExit(config, agent_client, web, "https://api.ipify.org")


def make_hub(settings, drivers) -> Hub:
    hub = Hub(settings, drivers, Store(settings.db_path))
    hub.startup()
    return hub


def rotate(hub: Hub, country: str | None = None) -> dict:
    async def scenario():
        op = hub.start_rotation("mobile-1", country)
        await hub.wait_operations()
        return hub.store.operation(op["id"])

    return asyncio.run(scenario())


def test_mobile_exit_defaults():
    config = MobileExitConfig(
        id="mobile-1", type="mobile", proxy_url="http://t:1", agent_url="http://t:2"
    )
    assert (config.pool, config.proxy_port, config.country) == ("mobile", None, None)
    assert config.recover_after_seconds is None
    assert config.cooldown_seconds == 60
    assert config.ready_timeout_seconds == 180
    assert not config.proxy_credentials
    assert VpnExitConfig.proxy_credentials


def test_exits_file_with_both_types(tmp_path):
    path = tmp_path / "exits.yaml"
    path.write_text(
        textwrap.dedent("""
        exits:
          - {id: vpn-1, type: vpn, pool: nordvpn, proxy_port: 8081,
             proxy_url: "http://g:8888", gluetun_control_url: "http://g:8000"}
          - {id: mobile-1, type: mobile, profile: mobile-1, proxy_url: "http://tunnel:13128",
             agent_url: "http://tunnel:19000", country: France}
        """)
    )
    assert [type(e) for e in load_exits(path, ["mobile-1"])] == [VpnExitConfig, MobileExitConfig]
    assert [e.id for e in load_exits(path)] == ["vpn-1"]


@pytest.mark.parametrize(
    "entry",
    [
        # Une sortie VPN garde son port publié obligatoire.
        '{id: vpn-1, type: vpn, pool: nordvpn, proxy_url: "http://g", gluetun_control_url: "x"}',
        '{id: mobile-1, type: mobile, proxy_url: "http://t"}',
        '{id: m, type: modem, proxy_url: "http://t"}',
    ],
)
def test_bad_exit_entries_are_refused(tmp_path, entry):
    path = tmp_path / "exits.yaml"
    path.write_text(f"exits:\n  - {entry}\n")
    with pytest.raises(ConfigError):
        load_exits(path)


def test_rotation_asks_the_phone(settings):
    agent = FakeAgent(PHONE_IPS)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent)}))

    assert op["status"] == "done"
    assert [op["result"]["old_ip"], op["result"]["new_ip"]] == PHONE_IPS[:2]
    assert op["result"]["country"] == "France"
    assert op["result"]["attempts"] == 1
    assert ("POST", "/rotate") in agent.requests


def test_rotation_waits_for_the_phone_through_the_outage(settings):
    agent = FakeAgent(PHONE_IPS, down_requests=4)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent, ready_timeout_seconds=1)}))

    assert (op["status"], op["result"]["new_ip"]) == ("done", PHONE_IPS[1])
    assert agent.rotation["status"] == "done"


def test_same_ip_after_rotation_does_not_wait_for_the_timeout(settings):
    # Le téléphone a fini sa rotation : la même IP est définitive, pas la peine d'attendre.
    agent = FakeAgent(PHONE_IPS[:1])
    hub = make_hub(settings, {"mobile-1": mobile_exit(agent, ready_timeout_seconds=30)})
    op = rotate(hub)

    assert op["status"] == "done"
    assert (op["result"]["changed"], op["result"]["attempts"]) == (False, 3)
    assert op["result"]["duration_seconds"] < 5
    assert agent.rotations == 3


def test_failed_phone_rotation(settings):
    agent = FakeAgent(PHONE_IPS, rotation_error="no network after airplane mode")
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent)}))

    assert op["status"] == "failed"
    assert op["error"] == "phone rotation failed: no network after airplane mode"


@pytest.mark.parametrize(
    ("code", "error"), [(409, "phone busy with another operation"), (503, "phone has no root")]
)
def test_phone_refusing_a_rotation(settings, code, error):
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(FakeAgent(PHONE_IPS, code))}))
    assert (op["status"], op["error"]) == ("failed", error)


def test_phone_never_finishing_its_rotation(settings):
    agent = FakeAgent(PHONE_IPS, down_requests=10_000)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent, ready_timeout_seconds=0.1)}))
    assert op["error"] == "phone did not finish its rotation in time"


def test_country_read_once_from_the_sim():
    agent = FakeAgent(PHONE_IPS, sim_country="be")
    driver = mobile_exit(agent)

    async def scenario():
        return [await driver.country(), await driver.country()]

    assert asyncio.run(scenario()) == ["Belgium", "Belgium"]
    assert agent.requests.count(("GET", "/status")) == 1


def test_country_from_config_wins():
    agent = FakeAgent(PHONE_IPS, sim_country="be")
    assert asyncio.run(mobile_exit(agent, country="fr").country()) == "France"
    assert agent.requests == []


def test_unknown_sim_country_refuses_a_country(settings):
    agent = FakeAgent(PHONE_IPS, sim_country=None)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent)}), "fr")

    assert "mobile exit is in an unknown country" in op["error"]
    assert ("POST", "/rotate") not in agent.requests


def test_rotation_not_available_on_the_phone(settings):
    agent = FakeAgent(PHONE_IPS, rotate_status=501)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent)}))

    assert op["status"] == "failed"
    assert op["error"] == "rotation not available on this phone"


def test_phone_cannot_change_country(settings):
    agent = FakeAgent(PHONE_IPS)
    op = rotate(make_hub(settings, {"mobile-1": mobile_exit(agent)}), "be")

    assert op["status"] == "failed"
    assert "mobile exit is in France" in op["error"]
    assert ("POST", "/rotate") not in agent.requests


def test_phone_only_on_request(settings, new_exit):
    async def scenario():
        hub = make_hub(
            settings,
            {
                "vpn-1": new_exit(FakeGluetun(["1.1.1.1"])),
                "mobile-1": mobile_exit(FakeAgent(PHONE_IPS)),
            },
        )
        await hub.check_all()
        vpn = await hub.session_for("a", "vpn", None, None)
        with pytest.raises(NoFreeExit):
            await hub.session_for("b", "vpn", None, None)
        phone = await hub.session_for("c", "mobile", "fr", None)
        return vpn.exit_id, phone.exit_id, hub.status["mobile-1"]

    vpn, phone, status = asyncio.run(scenario())
    assert (vpn, phone) == ("vpn-1", "mobile-1")
    assert (status.state, status.ip, status.country) == ("up", PHONE_IPS[0], "France")


def test_phone_session_in_another_country_fails(settings):
    async def scenario():
        hub = make_hub(settings, {"mobile-1": mobile_exit(FakeAgent(PHONE_IPS))})
        await hub.check_all()
        with pytest.raises(SessionFailed, match="country be not available in pool mobile"):
            await hub.session_for("a", "mobile", "be", None)
        return hub

    hub = asyncio.run(scenario())
    assert "mobile-1" not in hub.reserved


def test_phone_proxy_gets_no_credentials(settings):
    async def scenario():
        upstream = await FakeUpstream("phone", auth=False).start()
        hub = make_hub(
            settings, {"mobile-1": mobile_exit(FakeAgent(PHONE_IPS), proxy_url=upstream.url)}
        )
        await hub.check_all()
        proxy = EntryProxy(hub, settings, Guard(3, 600, 1800))
        port = await proxy.start("127.0.0.1", 0)
        auth = base64.b64encode(b"user-pool-mobile-session-a:password").decode()
        replies = []
        for head in (
            "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n",
            "GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n",
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"{head}Proxy-Authorization: Basic {auth}\r\n\r\n".encode())
            await writer.drain()
            replies.append(await reader.readuntil(b"\r\n\r\n"))
            writer.close()
        await proxy.close()
        await upstream.close()
        await hub.close()
        return replies, upstream.heads

    replies, heads = asyncio.run(scenario())
    assert all(reply.startswith(b"HTTP/1.1 200") for reply in replies)
    assert len(heads) == 2
    assert not any("Proxy-Authorization" in head for head in heads)


@pytest.fixture
def phone_client(settings, new_exit):
    agent = FakeAgent(PHONE_IPS)
    drivers = {"vpn-1": new_exit(FakeGluetun(["1.1.1.1"])), "mobile-1": mobile_exit(agent)}
    with TestClient(create_app(settings, drivers)) as client:
        yield client, agent


def test_api_shows_the_phone(phone_client, auth):
    client, agent = phone_client
    exits = {e["id"]: e for e in client.get("/exits", headers=auth).json()}
    assert (exits["mobile-1"]["type"], exits["mobile-1"]["pool"]) == ("mobile", "mobile")
    assert exits["mobile-1"]["proxy_port"] is None

    assert client.get("/exits/mobile-1/agent", headers=auth).json()["ip"] == PHONE_IPS[0]
    assert client.get("/exits/vpn-1/agent", headers=auth).status_code == 404
    assert client.get("/exits/nope/agent", headers=auth).status_code == 404
    assert client.get("/exits/mobile-1/agent").status_code == 401
    agent.reachable = False
    assert client.get("/exits/mobile-1/agent", headers=auth).status_code == 503
