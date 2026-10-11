import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from .config import Settings
from .store import Store, utcnow
from .vpn import ExitError, VpnExit

log = logging.getLogger("proxyhub")

# État affiché pendant une opération.
BUSY_STATE = {"rotate": "rotating", "recover": "recovering"}


class UnknownExit(Exception):
    pass


class ExitBusy(Exception):
    def __init__(self, operation_id: str):
        super().__init__(operation_id)
        self.operation_id = operation_id


class ExitInUse(Exception):
    def __init__(self, active_sessions: int):
        super().__init__(f"{active_sessions} active sessions")
        self.active_sessions = active_sessions


class ExitDown(Exception):
    pass


class CountryMismatch(Exception):
    def __init__(self, current: str):
        super().__init__(current)
        self.current = current


class CooldownActive(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry in {retry_after} s")
        self.retry_after = retry_after


@dataclass
class ExitStatus:
    # Santé mesurée : unknown, up ou down. Une opération en cours s'affiche à part.
    state: str = "unknown"
    ip: str | None = None
    country: str | None = None
    checked_at: str | None = None
    down_since: float | None = None
    last_recovery: float | None = None


def same_country(a: str | None, b: str | None) -> bool:
    return (a or "").casefold() == (b or "").casefold()


class Hub:
    def __init__(self, settings: Settings, exits: dict[str, VpnExit], store: Store):
        self.settings = settings
        self.exits = exits
        self.store = store
        self.status = {exit_id: ExitStatus() for exit_id in exits}
        self.running: dict[str, str] = {}
        self.busy_state: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()

    def startup(self) -> None:
        count = self.store.fail_unfinished_operations()
        if count:
            log.warning("marked %d unfinished operations as failed", count)
        for exit_id, status in self.status.items():
            status.ip = self.store.exit_row(exit_id)["ip"]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for driver in self.exits.values():
            await driver.aclose()

    def driver(self, exit_id: str) -> VpnExit:
        try:
            return self.exits[exit_id]
        except KeyError:
            raise UnknownExit(exit_id) from None

    def describe(self, exit_id: str) -> dict[str, Any]:
        config = self.driver(exit_id).config
        status = self.status[exit_id]
        return {
            "id": exit_id,
            "type": config.type,
            "state": self.busy_state.get(exit_id, status.state),
            "ip": status.ip,
            "country": status.country,
            "proxy_port": config.proxy_port,
            "last_rotation_at": self.store.exit_row(exit_id)["last_rotation_at"],
            "checked_at": status.checked_at,
            "operation_id": self.running.get(exit_id),
            "active_sessions": self.store.active_sessions(exit_id),
            "sessions_since_rotation": self._sessions_since_rotation(exit_id),
            "rotate_every_sessions": config.rotate_every_sessions,
        }

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(exit_id) for exit_id in self.exits]

    # Sessions

    async def open_session(self, exit_id: str, country: str | None = None) -> dict[str, Any]:
        config = self.driver(exit_id).config
        if exit_id in self.running:
            raise ExitBusy(self.running[exit_id])
        status = self.status[exit_id]
        if status.checked_at is None:
            await self._check(exit_id)
        if status.state == "down":
            raise ExitDown(exit_id)
        if country and status.country and not same_country(country, status.country):
            raise CountryMismatch(status.country)

        session = self.store.open_session(exit_id, config.session_ttl_seconds)
        log.info("session opened exit=%s session=%s", exit_id, session["id"])
        return {
            "session_id": session["id"],
            "exit": exit_id,
            "proxy_port": config.proxy_port,
            "ip": status.ip,
            "country": status.country,
            "expires_at": session["expires_at"],
        }

    def close_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.store.close_session(session_id)
        if session:
            log.info("session closed exit=%s session=%s", session["exit"], session_id)
            self._maybe_auto_rotate(session["exit"])
        return session

    def _sessions_since_rotation(self, exit_id: str) -> int:
        last = self.store.exit_row(exit_id)["last_rotation_at"]
        return self.store.closed_sessions_since(exit_id, last)

    def _maybe_auto_rotate(self, exit_id: str) -> None:
        every = self.exits[exit_id].config.rotate_every_sessions
        if not every or exit_id in self.running:
            return
        if self.store.active_sessions(exit_id):
            return
        if self._sessions_since_rotation(exit_id) < every:
            return
        # Délai minimal pas encore écoulé : le prochain balayage réessaiera.
        if self._cooldown_left(exit_id) > 0:
            return
        self._launch(exit_id, "rotate", {"country": None, "trigger": "auto"})

    # Balayage : expiration des sessions, rotation automatique, reconnexion

    def sweep(self) -> None:
        for exit_id in self.store.expire_sessions():
            log.info("session expired exit=%s", exit_id)
        for exit_id in self.exits:
            self._maybe_auto_rotate(exit_id)
            self._maybe_recover(exit_id)

    async def sweeper(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(self.settings.sweep_seconds)

    def _maybe_recover(self, exit_id: str) -> None:
        config = self.exits[exit_id].config
        status = self.status[exit_id]
        if config.recover_after_seconds is None or exit_id in self.running:
            return
        if status.state != "down" or status.down_since is None:
            return
        now = time.monotonic()
        if now - status.down_since < config.recover_after_seconds:
            return
        if status.last_recovery and now - status.last_recovery < config.recover_interval_seconds:
            return
        status.last_recovery = now
        self._launch(exit_id, "recover", {"trigger": "auto"})

    # Opérations

    def _cooldown_left(self, exit_id: str) -> float:
        last = self.store.exit_row(exit_id)["last_rotation_at"]
        if not last:
            return 0
        elapsed = (utcnow() - datetime.fromisoformat(last)).total_seconds()
        return self.exits[exit_id].config.cooldown_seconds - elapsed

    def start_rotation(
        self, exit_id: str, country: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        self.driver(exit_id)
        if exit_id in self.running:
            raise ExitBusy(self.running[exit_id])
        active = self.store.active_sessions(exit_id)
        if active and not force:
            raise ExitInUse(active)
        wait = self._cooldown_left(exit_id)
        if wait > 0:
            raise CooldownActive(math.ceil(wait))
        return self._launch(exit_id, "rotate", {"country": country, "trigger": "api"})

    def _launch(self, exit_id: str, op_type: str, params: dict) -> dict[str, Any]:
        op = self.store.create_operation(op_type, exit_id, params)
        self.running[exit_id] = op["id"]
        self.busy_state[exit_id] = BUSY_STATE[op_type]
        log.info("%s started exit=%s params=%s operation=%s", op_type, exit_id, params, op["id"])
        if op_type == "rotate":
            work = partial(self._rotate, exit_id, params["country"])
        else:
            work = partial(self._recover, exit_id)
        task = asyncio.create_task(self._run(exit_id, op, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    async def wait_operations(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(
        self, exit_id: str, op: dict, work: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        event = "rotation" if op["type"] == "rotate" else "recovery"
        try:
            result = await work()
        except Exception as e:
            error = str(e) or type(e).__name__
            log.warning("%s failed exit=%s error=%s", op["type"], exit_id, error)
            self.store.finish_operation(op["id"], "failed", error=error)
            self.store.add_history(exit_id, event, {"error": error})
        else:
            log.info("%s done exit=%s result=%s", op["type"], exit_id, result)
            self.store.finish_operation(op["id"], "done", result=result)
            self.store.add_history(exit_id, event, result)
            self.status[exit_id].ip = result["new_ip"]
            self.store.save_ip(exit_id, result["new_ip"])
        if op["type"] == "rotate":
            self.store.save_rotation_time(exit_id, utcnow())
        del self.running[exit_id]
        del self.busy_state[exit_id]
        await self._check(exit_id)

    async def _rotate(self, exit_id: str, country: str | None) -> dict[str, Any]:
        driver = self.exits[exit_id]
        started = time.monotonic()
        try:
            old_ip = await driver.measure_ip()
        except Exception:
            old_ip = self.status[exit_id].ip
        current = await driver.country()
        switch = country if country and not same_country(country, current) else None

        new_ip = None
        attempts = 0
        while attempts < driver.config.max_attempts:
            attempts += 1
            await driver.reconnect(switch if attempts == 1 else None)
            new_ip = await self._wait_for_ip(driver, old_ip)
            if new_ip and new_ip != old_ip:
                break
        if new_ip is None:
            raise ExitError("exit did not come back after rotation")

        return {
            "exit": exit_id,
            "country": country or current,
            "old_ip": old_ip,
            "new_ip": new_ip,
            "changed": new_ip != old_ip,
            "attempts": attempts,
            "duration_seconds": round(time.monotonic() - started, 1),
        }

    async def _recover(self, exit_id: str) -> dict[str, Any]:
        # Il suffit que la sortie remarche : une IP différente n'est pas exigée.
        driver = self.exits[exit_id]
        started = time.monotonic()
        await driver.reconnect()
        new_ip = await self._wait_for_ip(driver, None)
        if new_ip is None:
            raise ExitError("exit did not come back after reconnecting")
        return {
            "exit": exit_id,
            "old_ip": self.status[exit_id].ip,
            "new_ip": new_ip,
            "duration_seconds": round(time.monotonic() - started, 1),
        }

    async def _wait_for_ip(self, driver: VpnExit, old_ip: str | None) -> str | None:
        """Mesure jusqu'à obtenir une IP différente de `old_ip`, ou jusqu'au délai."""
        deadline = time.monotonic() + driver.config.ready_timeout_seconds
        last_ip = None
        while True:
            try:
                ip = await driver.measure_ip()
            except Exception:
                ip = None
            if ip:
                last_ip = ip
                if ip != old_ip:
                    return ip
            if time.monotonic() >= deadline:
                return last_ip
            await asyncio.sleep(driver.config.poll_seconds)

    # Surveillance

    async def monitor(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("monitor check failed")
            await asyncio.sleep(self.settings.monitor_seconds)

    async def check_all(self) -> None:
        for exit_id in self.exits:
            if exit_id not in self.running:
                await self._check(exit_id)
        self.store.purge(utcnow() - timedelta(days=self.settings.history_days))

    async def measure(self, exit_id: str) -> str | None:
        self.driver(exit_id)
        return await self._check(exit_id)

    async def _check(self, exit_id: str) -> str | None:
        driver = self.exits[exit_id]
        try:
            ip = await driver.measure_ip()
        except Exception as e:
            ip = None
            log.warning("ip check failed exit=%s error=%s", exit_id, type(e).__name__)
        try:
            country = await driver.country()
        except Exception:
            country = None
        # Une opération en cours gère elle-même l'état de la sortie.
        if exit_id in self.running:
            return ip

        status = self.status[exit_id]
        state = "up" if ip else "down"
        # Seuls les passages par "down" comptent : unknown -> up au démarrage est attendu.
        if state != status.state and "down" in (state, status.state):
            log.info("exit %s is %s", exit_id, state)
            self.store.add_history(exit_id, "state", {"state": state, "previous": status.state})
        if state == "down" and status.down_since is None:
            status.down_since = time.monotonic()
        if state == "up":
            status.down_since = None
        if ip and ip != status.ip:
            self.store.add_history(exit_id, "ip", {"ip": ip, "previous": status.ip})
            self.store.save_ip(exit_id, ip)
        status.state = state
        if ip:
            status.ip = ip
        if country:
            status.country = country
        status.checked_at = utcnow().isoformat()
        return ip
