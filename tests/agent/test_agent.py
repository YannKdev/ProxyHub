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
    assert body["rotation"] == {"engine": "airplane", "busy": False, "last": None}
    assert body["online"] is None


@pytest.mark.parametrize("path", ["/reboot/phone", "/reboot/host"])
def test_reboots_not_available_yet(agent_url, path):
    assert call(agent_url + path, "POST")[0] == 501


def test_unknown_routes(agent_url):
    assert call(f"{agent_url}/nope")[0] == 404
    assert call(f"{agent_url}/status", "POST")[0] == 404


# Rotation


class Tools:
    """Faux su et faux sv : chaque appel est noté dans `calls` ; une commande su contenant
    `fail` échoue."""

    def __init__(self, path):
        self.log = path / "calls"
        self.fail = path / "fail"
        self.fail.write_text("")
        self.su = self._script(path / "su", "su")
        self.sv = self._script(path / "sv", "sv")

    def _script(self, path, name: str) -> str:
        path.write_text(
            "#!/bin/sh\n"
            f'[ "$1" = -c ] && shift\n'
            f'echo "{name} $*" >> {self.log}\n'
            f'f=$(cat {self.fail}); [ -n "$f" ] && case "$*" in *"$f"*) exit 1;; esac\n'
            "exit 0\n"
        )
        path.chmod(0o755)
        return str(path)

    @property
    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def rotation_agent(tmp_path):
    """Agent rooté, rotation sans délai, service de mesure local."""
    tools = Tools(tmp_path)
    service = IpServer("198.51.100.8")
    agent = Agent(
        Config(
            port=0,
            su=tools.su,
            sv=tools.sv,
            tunnel_service="/svc/tunnel",
            hold_seconds=0,
            reply_delay_seconds=0,
            online_timeout_seconds=0,
            ip_check_url=service.url,
            ip6_check_url=service.url,
        )
    )
    agent.phone = {"root": True}
    agent.ip = "198.51.100.7"
    yield agent, tools, service
    service.server.shutdown()


def finish(agent: Agent) -> dict:
    deadline = threading.Event()
    for _ in range(100):
        if not agent.busy.locked():
            break
        deadline.wait(0.05)
    return agent.status()["rotation"]["last"]


AIRPLANE = "su cmd connectivity airplane-mode"


def test_rotation_cycles_airplane_mode(rotation_agent):
    agent, tools, _ = rotation_agent
    status, body = agent.start_rotation()
    last = finish(agent)

    assert status == 202
    assert tools.calls == [
        "sv -w 7 -v down /svc/tunnel",
        f"{AIRPLANE} enable",
        f"{AIRPLANE} disable",
        "sv -w 7 -v up /svc/tunnel",
    ]
    assert last["id"] == body["id"]
    assert (last["status"], last["old_ip"], last["new_ip"]) == (
        "done",
        "198.51.100.7",
        "198.51.100.8",
    )
    assert last["changed"] is True
    assert agent.ip == "198.51.100.8"


def test_airplane_mode_is_always_disabled(rotation_agent):
    agent, tools, _ = rotation_agent
    tools.fail.write_text("enable")
    agent.start_rotation()
    last = finish(agent)

    assert (last["status"], last["error"]) == ("failed", "airplane mode enable failed")
    assert tools.calls[-2:] == [f"{AIRPLANE} disable", "sv -w 7 -v up /svc/tunnel"]


def test_disabling_airplane_mode_is_retried(rotation_agent, monkeypatch):
    agent, tools, _ = rotation_agent
    monkeypatch.setattr("agent.time.sleep", lambda seconds: None)
    tools.fail.write_text("disable")
    agent.start_rotation()
    last = finish(agent)

    assert last["error"] == "airplane mode disable failed"
    assert tools.calls.count(f"{AIRPLANE} disable") == 3
    assert tools.calls[-1] == "sv -w 7 -v up /svc/tunnel"


def test_no_network_after_rotation(rotation_agent):
    agent, tools, service = rotation_agent
    service.ip = ""
    agent.start_rotation()
    last = finish(agent)

    assert (last["status"], last["error"]) == ("failed", "no network after airplane mode")
    assert last["changed"] is False
    assert tools.calls[-1] == "sv -w 7 -v up /svc/tunnel"


def test_one_operation_at_a_time(rotation_agent):
    agent, _, _ = rotation_agent
    agent.config.hold_seconds = 0.5
    assert agent.start_rotation()[0] == 202
    assert agent.start_rotation()[0] == 409
    finish(agent)
    assert agent.start_rotation()[0] == 202
    finish(agent)


def test_rotation_needs_root(rotation_agent):
    agent, tools, _ = rotation_agent
    agent.phone = {"root": False}
    assert agent.start_rotation()[0] == 503
    assert not any(call.startswith(AIRPLANE) for call in tools.calls)


def test_rotation_disabled(rotation_agent):
    agent, _, _ = rotation_agent
    agent.config.rotation = "none"
    assert agent.start_rotation()[0] == 501


def test_restore_after_an_interrupted_rotation(rotation_agent):
    agent, tools, _ = rotation_agent
    agent.phone = {"root": True, "airplane_mode": True}
    agent.restore()
    assert tools.calls == [f"{AIRPLANE} disable", "sv -w 7 -v up /svc/tunnel"]


def test_rotate_over_http(rotation_agent):
    agent, _, _ = rotation_agent
    server = AgentServer(agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    status, body = call(f"{url}/rotate", "POST")
    finish(agent)
    _, state = call(f"{url}/status")
    server.shutdown()

    assert status == 202
    assert state["rotation"]["last"]["id"] == body["id"]
    assert state["rotation"]["busy"] is False
