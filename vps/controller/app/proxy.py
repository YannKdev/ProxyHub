import asyncio
import base64
import binascii
import ipaddress
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import urlsplit

from .config import Settings
from .countries import COUNTRIES
from .guard import Guard
from .hub import DEFAULT_POOL, Hub, NoFreeExit, Session, SessionFailed

log = logging.getLogger("proxyhub")

BUFFER_LIMIT = 64 * 1024
HEAD_TIMEOUT = 30
UPSTREAM_TIMEOUT = 15
CHUNK = 64 * 1024
CHALLENGE = {"Proxy-Authenticate": 'Basic realm="proxyhub"'}
OPTION_PATTERNS = {
    "pool": re.compile(r"[a-z0-9]{1,32}"),
    "country": re.compile(r"[a-z]{2}"),
    "session": re.compile(r"[A-Za-z0-9_]{1,64}"),
    "hold": re.compile(r"[1-9][0-9]{0,4}"),
}
HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


class ProxyError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


@dataclass
class Options:
    session: str | None = None
    pool: str = DEFAULT_POOL
    country: str | None = None
    hold: int | None = None


@dataclass
class Request:
    method: str
    target: str
    version: str
    host: str
    port: int
    headers: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None


def parse_username(username: str, user: str) -> Options | None:
    """Options de `user-cle-valeur-...`. None si le nom ne commence pas par `user`."""
    if username == user:
        return Options()
    if not username.startswith(user + "-"):
        return None
    tokens = username[len(user) + 1 :].split("-")
    if len(tokens) % 2:
        raise ProxyError(400, "proxy options must be key-value pairs, e.g. user-session-a1")
    options = Options()
    seen: set[str] = set()
    for key, value in zip(tokens[::2], tokens[1::2], strict=True):
        pattern = OPTION_PATTERNS.get(key)
        if pattern is None:
            raise ProxyError(400, f"unknown proxy option {key!r}")
        if key in seen:
            raise ProxyError(400, f"proxy option {key!r} given twice")
        seen.add(key)
        if key in ("pool", "country"):
            value = value.lower()
        if not pattern.fullmatch(value):
            raise ProxyError(400, f"invalid value for proxy option {key!r}")
        if key == "country" and value not in COUNTRIES:
            raise ProxyError(400, f"unknown country code {value!r}")
        setattr(options, key, int(value) if key == "hold" else value)
    return options


def parse_head(head: bytes) -> Request:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise ProxyError(400, "bad request line")
    method, target, version = parts
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise ProxyError(400, "bad header line")
        headers.append((name.strip(), value.strip()))
    if method == "CONNECT":
        host, sep, port = target.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ProxyError(400, "CONNECT needs host:port")
        return Request(method, target, version, host.strip("[]"), int(port), headers)
    url = urlsplit(target)
    try:
        port = url.port or 80
    except ValueError:
        raise ProxyError(400, "bad port") from None
    if url.scheme != "http" or not url.hostname:
        raise ProxyError(400, "use CONNECT for https, or an absolute http:// URL")
    return Request(method, target, version, url.hostname, port, headers)


def status_of(head: bytes) -> int:
    parts = head.split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        return 0
    return int(parts[1])


class EntryProxy:
    """Port d'entrée unique : choisit la sortie d'après le nom d'utilisateur et relaie vers son
    proxy (celui de Gluetun pour une sortie VPN)."""

    def __init__(self, hub: Hub, settings: Settings, guard: Guard):
        self.hub = hub
        self.settings = settings
        self.guard = guard
        self.password = settings.proxy_password.encode()
        token = f"{settings.proxy_user}:{settings.proxy_password}".encode()
        self.upstream_auth = "Basic " + base64.b64encode(token).decode()
        self.pools = {d.config.pool for d in hub.exits.values()}
        self.pools |= {d.config.type for d in hub.exits.values()}
        self.server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()

    async def start(self, host: str, port: int) -> int:
        self.server = await asyncio.start_server(self.handle, host, port, limit=BUFFER_LIMIT)
        port = self.server.sockets[0].getsockname()[1]
        log.info("entry proxy listening on %s:%s", host, port)
        return port

    async def close(self) -> None:
        if self.server:
            self.server.close()
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._connections.add(task)
        peer = writer.get_extra_info("peername")
        client_ip = peer[0] if peer else "unknown"
        started = time.monotonic()
        host, exit_id, session_id, status = "-", "-", "-", 0
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                return
            request = parse_head(head)
            host = request.host
            options = self._authenticate(request, client_ip)
            self._check_destination(request)
            session = await self._session(options, client_ip)
            exit_id, session_id = session.exit_id or "-", session.id
            session.enter()
            try:
                status = await self._forward(request, reader, writer, session)
            finally:
                session.leave()
        except ProxyError as e:
            status = e.status
            await self._reply(writer, e.status, str(e), e.headers)
        except ConnectionError:
            status = status or 499
        except Exception:
            status = 500
            log.exception("proxy connection failed")
        finally:
            writer.close()
            if task:
                self._connections.discard(task)
            if status:
                log.info(
                    "proxy host=%s exit=%s session=%s status=%s duration=%.1fs",
                    host,
                    exit_id,
                    session_id,
                    status,
                    time.monotonic() - started,
                )

    def _authenticate(self, request: Request, client_ip: str) -> Options:
        blocked = self.guard.blocked_for(client_ip)
        if blocked:
            raise ProxyError(429, "too many failed authentications", {"Retry-After": str(blocked)})
        value = request.header("proxy-authorization")
        if not value:
            raise ProxyError(407, "proxy authentication required", CHALLENGE)
        scheme, _, token = value.partition(" ")
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            decoded = ""
        username, _, password = decoded.partition(":")
        options = None
        if scheme.lower() == "basic" and secrets.compare_digest(password.encode(), self.password):
            options = parse_username(username, self.settings.proxy_user)
        if options is None:
            self.guard.auth_failed(client_ip)
            raise ProxyError(407, "invalid proxy credentials", CHALLENGE)
        if options.pool not in self.pools:
            raise ProxyError(400, f"unknown pool {options.pool!r}")
        return options

    def _check_destination(self, request: Request) -> None:
        if request.port not in self.settings.proxy_connect_ports:
            raise ProxyError(403, f"port {request.port} not allowed")
        host = request.host.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            raise ProxyError(403, "destination not allowed")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if not address.is_global:
            raise ProxyError(403, "destination not allowed")

    async def _session(self, options: Options, client_ip: str) -> Session:
        # Sans -session-, une session par IP de client.
        name = options.session or f"ip:{client_ip}"
        try:
            return await asyncio.wait_for(
                self.hub.session_for(name, options.pool, options.country, options.hold),
                self.settings.proxy_wait_seconds,
            )
        except NoFreeExit as e:
            raise ProxyError(503, str(e), {"Retry-After": "10"}) from None
        except SessionFailed as e:
            raise ProxyError(502, str(e)) from None
        except TimeoutError:
            raise ProxyError(504, "exit not ready in time, retry") from None

    async def _forward(
        self,
        request: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: Session,
    ) -> int:
        upstream = urlsplit(self.hub.exits[session.exit_id].config.proxy_url)
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream.hostname, upstream.port or 80, limit=BUFFER_LIMIT),
                UPSTREAM_TIMEOUT,
            )
        except (OSError, TimeoutError):
            raise ProxyError(502, "exit unreachable") from None
        try:
            if request.method == "CONNECT":
                return await self._tunnel(request, reader, writer, up_reader, up_writer)
            up_writer.write(self._upstream_head(request))
            await up_writer.drain()
            return await self._relay(reader, writer, up_reader, up_writer, single_response=True)
        finally:
            up_writer.close()

    async def _tunnel(
        self,
        request: Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> int:
        up_writer.write(
            f"CONNECT {request.target} HTTP/1.1\r\nHost: {request.target}\r\n"
            f"Proxy-Authorization: {self.upstream_auth}\r\n\r\n".encode("latin-1")
        )
        await up_writer.drain()
        try:
            reply = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), UPSTREAM_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            raise ProxyError(502, "no answer from exit") from None
        status = status_of(reply)
        if status != 200:
            # 407 de la sortie : identifiants internes refusés, pas ceux du client.
            raise ProxyError(502 if status in (0, 401, 407) else status, f"exit answered {status}")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await self._relay(reader, writer, up_reader, up_writer)
        return 200

    def _upstream_head(self, request: Request) -> bytes:
        # Une requête par connexion : le client renvoie ses identifiants à chaque connexion.
        lines = [f"{request.method} {request.target} {request.version}"]
        lines += [f"{k}: {v}" for k, v in request.headers if k.lower() not in HOP_BY_HOP]
        lines += [f"Proxy-Authorization: {self.upstream_auth}", "Connection: close"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
        single_response: bool = False,
    ) -> int:
        """Copie dans les deux sens jusqu'à la fin des deux, une erreur, ou trop d'inactivité.
        `single_response` : s'arrête à la fin de la réponse (HTTP simple, une requête par
        connexion). Renvoie le code de la première réponse vue, 0 sinon."""
        idle = self.settings.session_idle_seconds
        last = time.monotonic()
        status = 0

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, watch: bool) -> None:
            nonlocal last, status
            while data := await src.read(CHUNK):
                if watch and not status:
                    status = status_of(data)
                last = time.monotonic()
                dst.write(data)
                await dst.drain()
            if dst.can_write_eof():
                dst.write_eof()

        async def watchdog() -> None:
            while time.monotonic() - last < idle:
                await asyncio.sleep(min(30, idle / 2))

        upload = asyncio.create_task(pipe(reader, up_writer, False))
        download = asyncio.create_task(pipe(up_reader, writer, True))
        pipes = {upload, download}
        guard = asyncio.create_task(watchdog())
        pending = pipes | {guard}
        try:
            while pipes & pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if guard in done or any(t.exception() for t in done if t is not guard):
                    break
                if single_response and download in done:
                    break
        finally:
            for task in pipes | {guard}:
                task.cancel()
            await asyncio.gather(*pipes, guard, return_exceptions=True)
        return status

    async def _reply(
        self, writer: asyncio.StreamWriter, status: int, message: str, headers: dict[str, str]
    ) -> None:
        body = (message + "\n").encode()
        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            phrase = "Error"
        lines = [
            f"HTTP/1.1 {status} {phrase}",
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
            *(f"{k}: {v}" for k, v in headers.items()),
        ]
        try:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
            await writer.drain()
        except ConnectionError:
            pass
