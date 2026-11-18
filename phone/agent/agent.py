"""Agent de la sortie mobile, dans Termux : état du téléphone sur 127.0.0.1:9000.

Le contrôleur le joint par le tunnel SSH (port 19000 du VPS). Bibliothèque standard seulement.
"""

import http.client
import ipaddress
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("agent")

PROPS = {
    "model": "ro.product.model",
    "android": "ro.build.version.release",
    "network": "gsm.network.type",
    "operator": "gsm.operator.alpha",
    # Pays de la SIM (code ISO), d'où vient l'IP mobile, même en itinérance.
    "sim_country": "gsm.sim.operator.iso-country",
}
PROPS_SCRIPT = "; ".join(f"echo {key}=$(getprop {prop})" for key, prop in PROPS.items())
# Une seule commande root par lecture : Magisk journalise chaque appel à su.
ROOT_SCRIPT = "; ".join(
    [
        "echo uid=$(id -u)",
        "echo airplane_mode=$(settings get global airplane_mode_on)",
        PROPS_SCRIPT,
        "dumpsys battery",
    ]
)
BATTERY_STATUS = {
    "1": "unknown",
    "2": "charging",
    "3": "discharging",
    "4": "not charging",
    "5": "full",
}


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 9000
    ip_check_url: str = "https://api.ipify.org"
    ip6_check_url: str = "https://api6.ipify.org"
    # Chaque mesure passe par la 4G (quelques Ko) : espacées quand tout va bien.
    ip_check_seconds: float = 300
    phone_check_seconds: float = 60
    su: str = "su"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = env.get(f"PROXYHUB_{field.name.upper()}")
            if raw:
                values[field.name] = field.type(raw)
        return cls(**values)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fetch_ip(url: str, timeout: float = 10) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return str(ipaddress.ip_address(response.read(64).decode().strip()))
    except (OSError, ValueError, http.client.HTTPException):
        return None


def parse_phone(output: str) -> dict[str, Any]:
    """Sortie de ROOT_SCRIPT : lignes "clé=valeur", puis celles de dumpsys battery."""
    values: dict[str, str] = {}
    battery: dict[str, str] = {}
    for line in output.splitlines():
        if line and not line[0].isspace() and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value.strip()
        elif ":" in line:
            key, _, value = line.strip().partition(":")
            battery[key.strip()] = value.strip()

    def number(key: str) -> int | None:
        value = battery.get(key, "")
        return int(value) if value.lstrip("-").isdigit() else None

    temperature = number("temperature")
    # Deux SIM : "fr,fr" ou ",fr".
    sim_country = next((c for c in values.get("sim_country", "").lower().split(",") if c), None)
    return {
        "root": values.get("uid") == "0",
        **{key: values.get(key) or None for key in PROPS},
        "sim_country": sim_country,
        "airplane_mode": {"1": True, "0": False}.get(values.get("airplane_mode", "")),
        "battery": {
            "level": number("level"),
            "temperature_c": temperature / 10 if temperature is not None else None,
            "plugged": any(
                battery.get(f"{s} powered") == "true" for s in ("AC", "USB", "Wireless")
            ),
            "status": BATTERY_STATUS.get(battery.get("status", "")),
        },
    }


def run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def read_phone(su: str) -> dict[str, Any]:
    phone = parse_phone(run([su, "-c", ROOT_SCRIPT]))
    if phone["root"]:
        return phone
    # Sans root : seulement les propriétés que Termux peut lire (pays de la SIM compris).
    props = parse_phone(run(["sh", "-c", PROPS_SCRIPT]))
    return {"root": False} | {key: props[key] for key in (*PROPS, "sim_country")}


class Agent:
    def __init__(self, config: Config):
        self.config = config
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.online: bool | None = None
        self.ip: str | None = None
        self.ipv6: str | None = None
        self.ip_checked_at: str | None = None
        self.offline_since: str | None = None
        self.phone: dict[str, Any] = {"root": False}
        self._stop = threading.Event()

    def check_ip(self) -> None:
        ip = fetch_ip(self.config.ip_check_url)
        ipv6 = fetch_ip(self.config.ip6_check_url)
        now = utcnow()
        with self.lock:
            if ip is None and self.online is not False:
                log.warning("offline")
                self.offline_since = now
            elif ip and self.online is False:
                log.info("online again ip=%s", ip)
            if ip:
                self.offline_since = None
            self.online = ip is not None
            self.ip, self.ipv6, self.ip_checked_at = ip, ipv6, now

    def check_phone(self) -> None:
        phone = read_phone(self.config.su)
        with self.lock:
            if phone["root"] != self.phone["root"]:
                log.info("root %s", "available" if phone["root"] else "unavailable")
            self.phone = phone

    def loop(self) -> None:
        next_ip = next_phone = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_phone:
                self.check_phone()
                next_phone = now + self.config.phone_check_seconds
            if now >= next_ip:
                self.check_ip()
                # Hors ligne : nouvel essai plus tôt.
                wait = (
                    self.config.ip_check_seconds if self.online else self.config.phone_check_seconds
                )
                next_ip = now + wait
            self._stop.wait(1)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "online": self.online,
                "ip": self.ip,
                "ipv6": self.ipv6,
                "ip_checked_at": self.ip_checked_at,
                "offline_since": self.offline_since,
                "phone": self.phone,
                "rotation": "none",
                "uptime_seconds": round(time.monotonic() - self.started),
            }


class Handler(BaseHTTPRequestHandler):
    server: "AgentServer"
    server_version = "proxyhub-agent"

    def do_GET(self) -> None:
        if self.path == "/status":
            self.reply(200, self.server.agent.status())
        else:
            self.reply(404, {"detail": "not found"})

    def do_POST(self) -> None:
        if self.path in ("/rotate", "/reboot/phone", "/reboot/host"):
            self.reply(501, {"detail": "not available on this agent yet"})
        else:
            self.reply(404, {"detail": "not found"})

    def reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Le contrôleur lit /status chaque minute : seules les actions et les erreurs comptent.
        if self.command != "GET" or code != 200:
            log.info("%s %s %s", self.command, self.path, code)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.warning(format, *args)


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, agent: Agent):
        self.agent = agent
        super().__init__((agent.config.host, agent.config.port), Handler)


def main() -> None:
    # svlogd ajoute la date à chaque ligne.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent = Agent(Config.from_env())
    threading.Thread(target=agent.loop, daemon=True).start()
    server = AgentServer(agent)
    log.info("agent listening on %s:%s", agent.config.host, agent.config.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
