import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from .config import Settings
from .countries import country_names, in_country
from .driver import CountryRejected, ExitDriver, ExitError
from .store import Store, utcnow

log = logging.getLogger("proxyhub")

# État affiché pendant une opération.
BUSY_STATE = {"rotate": "rotating", "recover": "recovering"}
DEFAULT_POOL = "vpn"


class UnknownExit(Exception):
    pass


class ExitBusy(Exception):
    def __init__(self, operation_id: str):
        super().__init__(operation_id)
        self.operation_id = operation_id


class ExitReserved(Exception):
    def __init__(self, session_id: str):
        super().__init__(f"exit reserved by session {session_id}")
        self.session_id = session_id


class CooldownActive(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry in {retry_after} s")
        self.retry_after = retry_after


class NoFreeExit(Exception):
    pass


class SessionFailed(Exception):
    pass


class SessionMoved(Exception):
    """Numéro d'IP plus petit que celui où la session est déjà passée."""

    def __init__(self, current: int):
        super().__init__(f"session already moved to ip {current}")
        self.current = current


class _ExitUnavailable(Exception):
    pass


@dataclass
class ExitStatus:
    # Santé mesurée : unknown, up ou down. Une opération en cours s'affiche à part.
    state: str = "unknown"
    ip: str | None = None
    country: str | None = None
    checked_at: str | None = None
    down_since: float | None = None
    last_recovery: float | None = None


@dataclass
class Session:
    """Réservation exclusive d'une sortie par un client du proxy d'entrée."""

    id: str
    key: tuple[str, str, str | None]
    label: str
    pool: str
    country: str | None
    idle_seconds: float
    created_at: str
    exit_id: str | None = None
    # Option -ip-<n> : un numéro plus grand demande une IP neuve pour la même session.
    ip_number: int = 0
    # Au moins une connexion relayée par la sortie actuelle : son IP a servi au client.
    forwarded: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    connections: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    failure: Exception | None = None

    def enter(self) -> None:
        self.connections += 1
        task = asyncio.current_task()
        if task:
            self.tasks.add(task)
        self.last_seen = time.monotonic()

    def leave(self) -> None:
        self.connections -= 1
        self.tasks.discard(asyncio.current_task())
        self.last_seen = time.monotonic()

    def describe(self) -> dict[str, Any]:
        idle = 0 if self.connections else round(time.monotonic() - self.last_seen)
        return {
            "id": self.id,
            "label": self.label,
            "exit": self.exit_id,
            "pool": self.pool,
            "country": self.country,
            "ip_number": self.ip_number,
            "connections": self.connections,
            "idle_seconds": idle,
            "release_after_seconds": round(self.idle_seconds),
            "created_at": self.created_at,
        }


class Hub:
    def __init__(self, settings: Settings, exits: dict[str, ExitDriver], store: Store):
        self.settings = settings
        self.exits = exits
        self.store = store
        self.status = {exit_id: ExitStatus() for exit_id in exits}
        self.running: dict[str, str] = {}
        self.busy_state: dict[str, str] = {}
        self.sessions: dict[tuple[str, str, str | None], Session] = {}
        self.reserved: dict[str, Session] = {}
        # Sorties libérées par une session, à faire tourner avant de les resservir.
        self.stale: set[str] = set()
        self.released_at: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._op_tasks: dict[str, asyncio.Task] = {}
        self._op_errors: dict[str, Exception] = {}

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

    def driver(self, exit_id: str) -> ExitDriver:
        try:
            return self.exits[exit_id]
        except KeyError:
            raise UnknownExit(exit_id) from None

    def describe(self, exit_id: str) -> dict[str, Any]:
        config = self.driver(exit_id).config
        status = self.status[exit_id]
        session = self.reserved.get(exit_id)
        return {
            "id": exit_id,
            "type": config.type,
            "pool": config.pool,
            "state": self.busy_state.get(exit_id, status.state),
            "ip": status.ip,
            "country": status.country,
            "proxy_port": config.proxy_port,
            "session_id": session.id if session else None,
            "last_rotation_at": self.store.exit_row(exit_id)["last_rotation_at"],
            "checked_at": status.checked_at,
            "operation_id": self.running.get(exit_id),
        }

    def describe_all(self) -> list[dict[str, Any]]:
        return [self.describe(exit_id) for exit_id in self.exits]

    # Sessions du proxy d'entrée

    def describe_sessions(self) -> list[dict[str, Any]]:
        return [session.describe() for session in self.sessions.values()]

    async def session_for(
        self,
        name: str,
        pool: str,
        country: str | None,
        hold_minutes: int | None,
        ip_number: int | None = None,
    ) -> Session:
        """Session existante pour cette clé, ou nouvelle réservation d'une sortie libre.

        `ip_number` plus grand que celui de la session : la session lâche sa sortie et en
        réserve une autre (ou la même après rotation), donc une IP neuve.
        """
        key = (name, pool, country)
        idle = self.settings.session_idle_seconds
        if hold_minutes:
            idle = max(idle, min(hold_minutes, self.settings.max_hold_minutes) * 60)
        session = self.sessions.get(key)
        if session is not None:
            # La session peut être en train de changer de sortie : attendre la fin.
            while not session.ready.is_set():
                await session.ready.wait()
            if isinstance(session.failure, NoFreeExit):
                raise NoFreeExit(str(session.failure))
            if session.failure:
                raise SessionFailed(str(session.failure))
            if ip_number is not None and ip_number < session.ip_number:
                raise SessionMoved(session.ip_number)
            if ip_number is None or ip_number == session.ip_number:
                session.idle_seconds = idle
                session.last_seen = time.monotonic()
                return session
            event = "renewed"
            old_exit = session.exit_id
            self._vacate(session)
            session.ready.clear()
        else:
            event, old_exit = "opened", None
            session = Session(
                id=uuid.uuid4().hex[:12],
                key=key,
                label=name,
                pool=pool,
                country=country,
                idle_seconds=idle,
                created_at=utcnow().isoformat(),
            )
            self.sessions[key] = session
        session.ip_number = ip_number or 0
        session.idle_seconds = idle
        await self._settle(session)
        log.info(
            "session %s session=%s exit=%s previous=%s pool=%s country=%s ip_number=%s",
            event,
            session.id,
            session.exit_id,
            old_exit,
            pool,
            country,
            session.ip_number,
        )
        session.last_seen = time.monotonic()
        return session

    async def replace_exit(self, session: Session, failed_exit: str) -> bool:
        """Première connexion ratée sur `failed_exit` : la session passe sur une autre sortie.

        Le client n'a encore rien reçu par cette sortie, donc changer d'IP ne casse rien.
        False si aucune autre sortie n'est libre : la session garde la sienne.
        """
        while not session.ready.is_set():
            await session.ready.wait()
        if self.sessions.get(session.key) is not session or session.failure:
            return False
        if session.exit_id != failed_exit:
            # Une autre connexion de la session l'a déjà déplacée.
            return True
        self._spawn(self._check(failed_exit))
        exclude = frozenset({failed_exit})
        names = country_names(session.country) if session.country else None
        if not self._candidates(session.pool, names, exclude):
            return False
        self._vacate(session)
        session.ready.clear()
        await self._settle(session, exclude)
        log.info(
            "session moved session=%s exit=%s previous=%s reason=exit failed",
            session.id,
            session.exit_id,
            failed_exit,
        )
        return True

    async def _settle(self, session: Session, exclude: frozenset[str] = frozenset()) -> None:
        """Réserve une sortie pour la session. En cas d'échec, la session disparaît et les
        requêtes qui l'attendaient reçoivent la même erreur."""
        try:
            await self._assign(session, exclude)
        except BaseException as e:
            session.failure = e if isinstance(e, Exception) else SessionFailed("interrupted")
            if self.sessions.get(session.key) is session:
                del self.sessions[session.key]
            raise
        finally:
            session.ready.set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def release(self, session: Session, reason: str) -> None:
        if self.sessions.get(session.key) is not session:
            return
        del self.sessions[session.key]
        exit_id = session.exit_id
        self._vacate(session)
        log.info("session released session=%s exit=%s reason=%s", session.id, exit_id, reason)

    def _vacate(self, session: Session) -> None:
        """Libère la sortie de la session (à faire tourner) et coupe ses connexions."""
        exit_id = session.exit_id
        self._unreserve(session)
        if exit_id:
            self.stale.add(exit_id)
            self.released_at[exit_id] = time.monotonic()
        for task in list(session.tasks):
            task.cancel()

    def release_by_id(self, session_id: str) -> bool:
        for session in list(self.sessions.values()):
            if session.id == session_id:
                self.release(session, "api")
                return True
        return False

    def _unreserve(self, session: Session) -> None:
        if session.exit_id and self.reserved.get(session.exit_id) is session:
            del self.reserved[session.exit_id]
        session.exit_id = None

    def _candidates(
        self, pool: str, names: list[str] | None, exclude: frozenset[str] = frozenset()
    ) -> list[str]:
        """Sorties libres du groupe, celles utilisables tout de suite d'abord. Une sortie
        jamais mesurée (unknown) passe après celles vues up."""

        def cost(exit_id: str) -> tuple[bool, bool, bool, float]:
            fresh = exit_id not in self.stale
            country_ok = names is None or in_country(self.status[exit_id].country, names)
            return (
                exit_id in self.running,
                self.status[exit_id].state != "up",
                not (fresh and country_ok),
                self.released_at.get(exit_id, 0),
            )

        free = [
            exit_id
            for exit_id, driver in self.exits.items()
            if pool in (driver.config.pool, driver.config.type)
            and exit_id not in self.reserved
            and exit_id not in exclude
            and self.status[exit_id].state != "down"
        ]
        return sorted(free, key=cost)

    async def _assign(self, session: Session, exclude: frozenset[str] = frozenset()) -> None:
        names = country_names(session.country) if session.country else None
        refused: list[Exception] = []
        for exit_id in self._candidates(session.pool, names, exclude):
            if exit_id in self.reserved:
                continue
            self.reserved[exit_id] = session
            session.exit_id = exit_id
            try:
                await self._prepare(exit_id, names)
                session.forwarded = False
                return
            except (CountryRejected, _ExitUnavailable, SessionFailed) as e:
                # Pays inconnu de ce fournisseur, sortie tombée entre-temps ou rotation
                # ratée : la sortie suivante.
                refused.append(e)
                self._unreserve(session)
            except BaseException:
                self._unreserve(session)
                raise
        failed = [e for e in refused if isinstance(e, SessionFailed)]
        if failed:
            raise failed[-1]
        if any(isinstance(e, CountryRejected) for e in refused):
            raise SessionFailed(f"country {session.country} not available in pool {session.pool}")
        raise NoFreeExit(f"no free exit in pool {session.pool}")

    async def _prepare(self, exit_id: str, names: list[str] | None) -> None:
        """Rend une sortie réservée utilisable : IP neuve après libération, bon pays."""
        while exit_id in self.running:
            await self._wait_operation(self.running[exit_id])
        if self.status[exit_id].state == "down":
            raise _ExitUnavailable(exit_id)
        switch = names is not None and not in_country(self.status[exit_id].country, names)
        if not switch and exit_id not in self.stale:
            return
        op = self._launch(
            exit_id, "rotate", {"country": names if switch else None, "trigger": "proxy"}
        )
        final = await self._wait_operation(op["id"])
        error = self._op_errors.pop(op["id"], None)
        if final["status"] == "failed":
            if isinstance(error, CountryRejected):
                raise error
            raise SessionFailed(final["error"])

    async def _wait_operation(self, op_id: str) -> dict[str, Any]:
        task = self._op_tasks.get(op_id)
        if task:
            # shield : un client qui abandonne n'interrompt pas l'opération.
            await asyncio.shield(task)
        else:
            await asyncio.sleep(0.05)
        return self.store.operation(op_id)

    # Balayage : sessions inactives, rotation des sorties libérées, reconnexion

    def sweep(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            idle = now - session.last_seen
            if session.ready.is_set() and not session.connections and idle > session.idle_seconds:
                self.release(session, "idle")
        for exit_id in self.exits:
            self._maybe_refresh(exit_id)
            self._maybe_recover(exit_id)

    async def sweeper(self) -> None:
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("sweep failed")
            await asyncio.sleep(self.settings.sweep_seconds)

    def _maybe_refresh(self, exit_id: str) -> None:
        if exit_id not in self.stale or exit_id in self.reserved or exit_id in self.running:
            return
        if self.status[exit_id].state == "down" or self._cooldown_left(exit_id) > 0:
            return
        self._launch(exit_id, "rotate", {"country": None, "trigger": "release"})

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
        session = self.reserved.get(exit_id)
        if session and not force:
            raise ExitReserved(session.id)
        wait = self._cooldown_left(exit_id)
        if wait > 0:
            raise CooldownActive(math.ceil(wait))
        names = country_names(country) if country else None
        return self._launch(exit_id, "rotate", {"country": names, "trigger": "api"})

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
        self._op_tasks[op["id"]] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _, op_id=op["id"]: self._op_tasks.pop(op_id, None))
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
            if op["params"].get("trigger") == "proxy":
                self._op_errors[op["id"]] = e
        else:
            log.info("%s done exit=%s result=%s", op["type"], exit_id, result)
            self.store.finish_operation(op["id"], "done", result=result)
            self.store.add_history(exit_id, event, result)
            self.status[exit_id].ip = result["new_ip"]
            self.store.save_ip(exit_id, result["new_ip"])
        if op["type"] == "rotate":
            self.store.save_rotation_time(exit_id, utcnow())
            self.stale.discard(exit_id)
        del self.running[exit_id]
        del self.busy_state[exit_id]
        await self._check(exit_id)

    async def _rotate(self, exit_id: str, countries: list[str] | None) -> dict[str, Any]:
        driver = self.exits[exit_id]
        started = time.monotonic()
        try:
            old_ip = await driver.measure_ip()
        except Exception:
            old_ip = self.status[exit_id].ip
        current = await driver.country()
        switch = countries if countries and not in_country(current, countries) else None
        # Deux sorties du même fournisseur peuvent tomber sur le même serveur, donc la même IP.
        taken = {s.ip: other for other, s in self.status.items() if other != exit_id and s.ip}
        avoid = set(taken) | {old_ip}

        new_ip = None
        attempts = 0
        while attempts < driver.config.max_attempts:
            attempts += 1
            await driver.reconnect(switch if attempts == 1 else None)
            new_ip = await self._wait_for_ip(driver, avoid)
            if new_ip and new_ip not in avoid:
                break
        if new_ip is None and switch and current:
            # Pays injoignable : revenir au pays d'avant plutôt que laisser la sortie coupée.
            await driver.reconnect([current])
            back = await self._wait_for_ip(driver, set())
            error = f"exit did not come back in {switch[0]}, switched back to {current}"
            raise ExitError(error if back else f"{error}, still down")
        if new_ip is None:
            raise ExitError("exit did not come back after rotation")

        return {
            "exit": exit_id,
            "country": switch[0] if switch else current,
            "old_ip": old_ip,
            "new_ip": new_ip,
            "changed": new_ip != old_ip,
            "shared_with": [taken[new_ip]] if new_ip in taken else [],
            "attempts": attempts,
            "duration_seconds": round(time.monotonic() - started, 1),
        }

    async def _recover(self, exit_id: str) -> dict[str, Any]:
        # Il suffit que la sortie remarche : une IP différente n'est pas exigée.
        driver = self.exits[exit_id]
        started = time.monotonic()
        await driver.reconnect()
        new_ip = await self._wait_for_ip(driver, set())
        if new_ip is None:
            raise ExitError("exit did not come back after reconnecting")
        return {
            "exit": exit_id,
            "old_ip": self.status[exit_id].ip,
            "new_ip": new_ip,
            "duration_seconds": round(time.monotonic() - started, 1),
        }

    async def _wait_for_ip(self, driver: ExitDriver, avoid: set[str | None]) -> str | None:
        """Mesure jusqu'à obtenir une IP hors de `avoid`, ou jusqu'au délai. Si la sortie a fini
        de changer d'IP quand reconnect() rend la main (téléphone), la première IP suffit."""
        deadline = time.monotonic() + driver.config.ready_timeout_seconds
        last_ip = None
        while True:
            try:
                ip = await driver.measure_ip()
            except Exception:
                ip = None
            if ip:
                last_ip = ip
                if ip not in avoid or driver.config.reconnect_settles:
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
