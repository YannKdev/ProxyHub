import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agent import Agent, AgentServer, Config, parse_phone, read_phone

PHONE_OUTPUT = """uid=0
airplane_mode=0
model=Phone 1
android=11
network=LTE
operator=Carrier
sim_country=fr
Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  health: 2
  level: 81
  temperature: 312
"""


@pytest.fixture
def fake_su(tmp_path):
    """Faux su : affiche `output` et sort avec `code`, quelle que soit la commande."""

    def make(output: str, code: int = 0) -> str:
        path = tmp_path / "su"
        (tmp_path / "out").write_text(output)
        path.write_text(f"#!/bin/sh\ncat {tmp_path / 'out'}\nexit {code}\n")
        path.chmod(0o755)
        return str(path)

    return make


class IpServer:
    """Service de mesure d'IP local : renvoie `ip`, ou 500 si `ip` est vide."""

    def __init__(self, ip: str):
        self.ip = ip
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = outer.ip.encode()
                self.send_response(200 if body else 500)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_config_from_env():
    config = Config.from_env({"PROXYHUB_PORT": "9100", "PROXYHUB_IP_CHECK_SECONDS": "30"})
    assert (config.port, config.ip_check_seconds, config.host) == (9100, 30.0, "127.0.0.1")


def test_parse_phone():
    phone = parse_phone(PHONE_OUTPUT)
    assert phone == {
        "root": True,
        "model": "Phone 1",
        "android": "11",
        "network": "LTE",
        "operator": "Carrier",
        "sim_country": "fr",
        "airplane_mode": False,
        "battery": {"level": 81, "temperature_c": 31.2, "plugged": True, "status": "charging"},
    }


def test_read_phone_through_su(fake_su):
    assert read_phone(fake_su(PHONE_OUTPUT))["model"] == "Phone 1"


def test_dual_sim_country():
    assert parse_phone("uid=0\nsim_country=,FR\n")["sim_country"] == "fr"


@pytest.mark.parametrize(
    ("output", "code"), [("", 1), ("uid=10123\nmodel=Phone 1\n", 0)], ids=["denied", "not-root"]
)
def test_read_phone_without_root(fake_su, output, code):
    # Sans root, seulement les propriétés lues sans su (aucune ici : pas de getprop).
    phone = read_phone(fake_su(output, code))
    assert phone["root"] is False
    assert "battery" not in phone
    assert (phone["model"], phone["sim_country"]) == (None, None)


def test_read_phone_without_su(tmp_path):
    assert read_phone(str(tmp_path / "missing"))["root"] is False


def test_ip_check_tracks_outages():
    service = IpServer("198.51.100.7")
    agent = Agent(Config(ip_check_url=service.url, ip6_check_url=service.url + "6"))
    agent.check_ip()
    assert (agent.online, agent.ip, agent.offline_since) == (True, "198.51.100.7", None)

    service.ip = ""
    agent.check_ip()
    since = agent.offline_since
    assert (agent.online, agent.ip, since is not None) == (False, None, True)
    agent.check_ip()
    assert agent.offline_since == since

    service.ip = "198.51.100.8"
    agent.check_ip()
    assert (agent.online, agent.ip, agent.offline_since) == (True, "198.51.100.8", None)
    service.server.shutdown()


def test_ip_check_rejects_garbage():
    service = IpServer("<html>")
    agent = Agent(Config(ip_check_url=service.url, ip6_check_url=service.url))
    agent.check_ip()
    assert agent.online is False
    service.server.shutdown()


@pytest.fixture
def agent_url(fake_su):
    agent = Agent(Config(port=0, su=fake_su(PHONE_OUTPUT)))
    agent.check_phone()
    server = AgentServer(agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(url: str, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_status(agent_url):
    status, body = call(f"{agent_url}/status")
    assert status == 200
    assert body["phone"]["root"] is True
    assert body["phone"]["battery"]["level"] == 81
    assert body["rotation"] == "none"
    assert body["online"] is None


@pytest.mark.parametrize("path", ["/rotate", "/reboot/phone", "/reboot/host"])
def test_operations_not_available_yet(agent_url, path):
    assert call(agent_url + path, "POST")[0] == 501


def test_unknown_routes(agent_url):
    assert call(f"{agent_url}/nope")[0] == 404
    assert call(f"{agent_url}/status", "POST")[0] == 404
