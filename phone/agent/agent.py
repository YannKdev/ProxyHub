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
import uuid
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
TERMUX_PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")


class RotationError(Exception):
    pass


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
    # Rotation : "airplane" (mode avion, root nécessaire) ou "none".
    rotation: str = "airplane"
    hold_seconds: float = 5
    online_timeout_seconds: float = 60
    # Laisse partir la réponse 202 par le tunnel avant de le fermer.
    reply_delay_seconds: float = 1
    sv: str = "sv"
    tunnel_service: str = f"{TERMUX_PREFIX}/var/service/proxyhub-tunnel"

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
        # Une seule opération à la fois (rotation, plus tard réparation et redémarrages).
        self.busy = threading.Lock()
        # Rotation en cours ou dernière rotation.
        self.rotation: dict[str, Any] | None = None
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
            # Pendant une opération, la coupure est voulue : pas de mesure.
            if now >= next_ip and not self.busy.locked():
                self.check_ip()
                # Hors ligne : nouvel essai plus tôt.
                wait = (
                    self.config.ip_check_seconds if self.online else self.config.phone_check_seconds
                )
                next_ip = now + wait
            self._stop.wait(1)

    def stop(self) -> None:
        self._stop.set()

    def root_command(self, command: str) -> bool:
        try:
            done = subprocess.run(
                [self.config.su, "-c", command], capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0

    def tunnel(self, action: str) -> None:
        """ "down" ferme proprement la session SSH : le VPS libère ses ports tout de suite, au
        lieu de les garder jusqu'à 30 s après une coupure brutale."""
        try:
            subprocess.run(
                [self.config.sv, "-w", "7", "-v", action, self.config.tunnel_service],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("tunnel %s failed: %s", action, e)

    def airplane(self, enabled: bool) -> None:
        state = "enable" if enabled else "disable"
        # Désactiver doit réussir, sinon le téléphone reste hors réseau : plusieurs essais.
        for attempt in range(1 if enabled else 3):
            if attempt:
                time.sleep(2)
            if self.root_command(f"cmd connectivity airplane-mode {state}"):
                return
        raise RotationError(f"airplane mode {state} failed")

    def restore(self) -> None:
        """Au démarrage : défait une opération interrompue (agent arrêté en pleine rotation)."""
        if self.phone.get("airplane_mode"):
            log.warning("airplane mode left on, disabling it")
            self.airplane(False)
        self.tunnel("up")

    def start_rotation(self) -> tuple[int, dict[str, Any]]:
        if self.config.rotation != "airplane":
            return 501, {"detail": "rotation disabled on this phone"}
        if not self.phone["root"]:
            self.check_phone()
            if not self.phone["root"]:
                return 503, {"detail": "root not available"}
        if not self.busy.acquire(blocking=False):
            return 409, {"detail": "an operation is already running"}
        rotation = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "started_at": utcnow(),
            "old_ip": self.ip,
        }
        with self.lock:
            self.rotation = rotation
        threading.Thread(target=self._rotate, args=(rotation,), daemon=True).start()
        return 202, {"id": rotation["id"]}

    def _rotate(self, rotation: dict[str, Any]) -> None:
        log.info("rotation started id=%s", rotation["id"])
        started = time.monotonic()
        result: dict[str, Any] = {"status": "done", "new_ip": None, "error": None}
        try:
            time.sleep(self.config.reply_delay_seconds)
            self.tunnel("down")
            try:
                self.airplane(True)
                time.sleep(self.config.hold_seconds)
            finally:
                self.airplane(False)
            result["new_ip"] = self._wait_online()
            if result["new_ip"] is None:
                raise RotationError("no network after airplane mode")
        except Exception as e:
            result.update(status="failed", error=str(e) or type(e).__name__)
        finally:
            self.tunnel("up")
            if result["new_ip"]:
                self.check_ip()
            result["changed"] = result["new_ip"] not in (None, rotation["old_ip"])
            result["finished_at"] = utcnow()
            result["duration_seconds"] = round(time.monotonic() - started, 1)
            with self.lock:
                rotation.update(result)
            self.busy.release()
            log.info("rotation %s id=%s %s", result["status"], rotation["id"], result)

    def _wait_online(self) -> str | None:
        deadline = time.monotonic() + self.config.online_timeout_seconds
        while True:
            ip = fetch_ip(self.config.ip_check_url, timeout=5)
            if ip or time.monotonic() >= deadline:
                return ip
            time.sleep(2)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "online": self.online,
                "ip": self.ip,
                "ipv6": self.ipv6,
                "ip_checked_at": self.ip_checked_at,
                "offline_since": self.offline_since,
                "phone": self.phone,
                "rotation": {
                    "engine": self.config.rotation,
                    "busy": self.busy.locked(),
                    "last": dict(self.rotation) if self.rotation else None,
                },
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
        if self.path == "/rotate":
            self.reply(*self.server.agent.start_rotation())
        elif self.path in ("/reboot/phone", "/reboot/host"):
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
    agent.check_phone()
    try:
        agent.restore()
    except RotationError as e:
        log.error("%s", e)
    threading.Thread(target=agent.loop, daemon=True).start()
    server = AgentServer(agent)
    log.info("agent listening on %s:%s", agent.config.host, agent.config.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
