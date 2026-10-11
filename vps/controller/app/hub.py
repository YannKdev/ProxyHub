import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .store import Store, utcnow
from .vpn import ExitError, VpnExit

log = logging.getLogger("proxyhub")


class UnknownExit(Exception):
    pass


class ExitBusy(Exception):
    pass


class CooldownActive(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry in {retry_after} s")
        self.retry_after = retry_after


@dataclass
class ExitStatus:
    state: str = "unknown"
    ip: str | None = None
    country: str | None = None
    checked_at: str | None = None


class Hub:
    def __init__(self, settings: Settings, exits: dict[str, VpnExit], store: Store):
        self.settings = settings
        self.exits = exits
        self.store = store
        self.status = {exit_id: ExitStatus() for exit_id in exits}
        self.running: dict[str, str] = {}
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
            "state": status.state,
            "ip": status.ip,
            "country": status.country,
            "proxy_port": config.proxy_port,
            "last_rotation_at": self.store.exit_row(exit_id)["last_rotation_at"],
            "checked_at": status.checked_at,
            "operation_id": self.running.get(exit_id),
        }

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(exit_id) for exit_id in self.exits]

    def start_rotation(self, exit_id: str, country: str | None = None) -> dict[str, Any]:
        config = self.driver(exit_id).config
        if exit_id in self.running:
            raise ExitBusy(exit_id)
        last = self.store.exit_row(exit_id)["last_rotation_at"]
        if last:
            elapsed = (utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < config.cooldown_seconds:
                raise CooldownActive(math.ceil(config.cooldown_seconds - elapsed))

        op = self.store.create_operation("rotate", exit_id, {"country": country})
        self.running[exit_id] = op["id"]
        self.status[exit_id].state = "rotating"
        log.info("rotation started exit=%s country=%s operation=%s", exit_id, country, op["id"])
        task = asyncio.create_task(self._run_rotation(exit_id, op["id"], country))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    async def wait_operations(self) -> None:
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_rotation(self, exit_id: str, op_id: str, country: str | None) -> None:
        try:
            result = await self._rotate(exit_id, country)
        except Exception as e:
            error = str(e) or type(e).__name__
            log.warning("rotation failed exit=%s error=%s", exit_id, error)
            self.store.finish_operation(op_id, "failed", error=error)
            self.store.add_history(exit_id, "rotation", {"error": error})
        else:
            log.info(
                "rotation done exit=%s changed=%s attempts=%s duration=%ss",
                exit_id,
                result["changed"],
                result["attempts"],
                result["duration_seconds"],
            )
            self.store.finish_operation(op_id, "done", result=result)
            self.store.add_history(exit_id, "rotation", result)
            self.status[exit_id].ip = result["new_ip"]
            self.store.save_ip(exit_id, result["new_ip"])
        self.store.save_rotation_time(exit_id, utcnow())
        del self.running[exit_id]
        await self._check(exit_id)

    async def _rotate(self, exit_id: str, country: str | None) -> dict[str, Any]:
        driver = self.exits[exit_id]
        started = time.monotonic()
        try:
            old_ip = await driver.measure_ip()
        except Exception:
            old_ip = self.status[exit_id].ip
        current = await driver.country()
        switch = country if country and country.casefold() != (current or "").casefold() else None

        new_ip = None
        attempts = 0
        while attempts < driver.config.max_attempts:
            attempts += 1
            await driver.reconnect(switch if attempts == 1 else None)
            new_ip = await self._wait_for_new_ip(driver, old_ip)
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

    async def _wait_for_new_ip(self, driver: VpnExit, old_ip: str | None) -> str | None:
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
        # Une rotation en cours gère elle-même l'état de la sortie.
        if exit_id in self.running:
            return ip

        status = self.status[exit_id]
        state = "up" if ip else "down"
        # Seuls les passages par "down" comptent : "up" après une rotation est attendu.
        if state != status.state and "down" in (state, status.state):
            log.info("exit %s is %s", exit_id, state)
            self.store.add_history(exit_id, "state", {"state": state, "previous": status.state})
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
