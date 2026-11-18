import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import (
    AnyExitConfig,
    ConfigError,
    MobileExitConfig,
    Settings,
    load_exits,
    load_settings,
)
from .driver import ExitDriver
from .guard import Guard
from .hub import CooldownActive, ExitBusy, ExitReserved, Hub, UnknownExit
from .mobile import MobileExit, make_mobile_exit
from .proxy import EntryProxy
from .store import Store
from .vpn import make_vpn_exit

log = logging.getLogger("proxyhub")

Country = Annotated[str | None, Query(pattern=r"^[A-Za-z][A-Za-z .'-]{1,39}$")]


def make_exit(config: AnyExitConfig, settings: Settings) -> ExitDriver:
    if isinstance(config, MobileExitConfig):
        return make_mobile_exit(config, settings)
    return make_vpn_exit(config, settings)


def create_app(
    settings: Settings | None = None, exits: dict[str, ExitDriver] | None = None
) -> FastAPI:
    if settings is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = settings or load_settings()
        if exits is None:
            configs = load_exits(settings.exits_file, settings.compose_profiles)
            exits = {c.id: make_exit(c, settings) for c in configs}
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1) from None
    log.info("exits loaded: %s", ", ".join(exits) or "none")

    hub = Hub(settings, exits, Store(settings.db_path))
    # Partagé par l'API et le proxy d'entrée : les échecs des deux comptent ensemble.
    guard = Guard(
        settings.auth_max_failures,
        settings.auth_failure_window_seconds,
        settings.auth_block_seconds,
    )
    proxy = EntryProxy(hub, settings, guard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.startup()
        loops = []
        if settings.monitor_seconds > 0:
            loops.append(asyncio.create_task(hub.monitor()))
        if settings.sweep_seconds > 0:
            loops.append(asyncio.create_task(hub.sweeper()))
        if settings.proxy_port is not None:
            await proxy.start("0.0.0.0", settings.proxy_port)
        yield
        await proxy.close()
        for task in loops:
            task.cancel()
        await hub.close()

    app = FastAPI(
        title="proxyhub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.hub = hub
    bearer = HTTPBearer(auto_error=False)
    token = settings.api_token.encode()

    def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), token
        ):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def error(status: int, detail: str, **extra) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": detail, **extra})

    app.state.guard = guard
    app.state.proxy = proxy

    @app.middleware("http")
    async def block_bad_tokens(request: Request, call_next) -> Response:
        if request.url.path == "/health":
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        blocked_for = guard.blocked_for(ip)
        if blocked_for:
            response = error(429, "too many failed authentications")
            response.headers["Retry-After"] = str(blocked_for)
            return response
        response = await call_next(request)
        # Seul un jeton faux compte : une requête sans jeton n'essaie rien.
        if response.status_code == 401 and "authorization" in request.headers:
            guard.auth_failed(ip)
        return response

    @app.exception_handler(UnknownExit)
    async def unknown_exit(request: Request, exc: UnknownExit) -> JSONResponse:
        return error(404, "unknown exit")

    @app.exception_handler(ExitBusy)
    async def exit_busy(request: Request, exc: ExitBusy) -> JSONResponse:
        return error(
            409, "an operation is already running on this exit", operation_id=exc.operation_id
        )

    @app.exception_handler(ExitReserved)
    async def exit_reserved(request: Request, exc: ExitReserved) -> JSONResponse:
        return error(
            409,
            f"exit reserved by session {exc.session_id}, use force=true to rotate anyway",
            session_id=exc.session_id,
        )

    @app.exception_handler(CooldownActive)
    async def cooldown(request: Request, exc: CooldownActive) -> JSONResponse:
        response = error(429, f"cooldown active, retry in {exc.retry_after} s")
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    api = APIRouter(dependencies=[Depends(require_token)])

    @api.get("/exits")
    async def list_exits() -> list[dict]:
        return hub.describe_all()

    @api.get("/exits/{exit_id}/ip")
    async def exit_ip(exit_id: str) -> dict:
        ip = await hub.measure(exit_id)
        if ip is None:
            raise HTTPException(status_code=503, detail="exit unreachable")
        return {"exit": exit_id, "ip": ip}

    @api.get("/exits/{exit_id}/agent")
    async def exit_agent(exit_id: str) -> dict:
        driver = hub.driver(exit_id)
        if not isinstance(driver, MobileExit):
            raise HTTPException(status_code=404, detail="not a mobile exit")
        try:
            return await driver.status()
        except Exception as e:
            raise HTTPException(status_code=503, detail="phone agent unreachable") from e

    @api.post("/exits/{exit_id}/rotate", status_code=202)
    async def rotate(exit_id: str, country: Country = None, force: bool = False) -> dict:
        op = hub.start_rotation(exit_id, country, force)
        return {"operation_id": op["id"], "status": op["status"]}

    @api.get("/operations/{operation_id}")
    async def operation(operation_id: str) -> dict:
        op = hub.store.operation(operation_id)
        if op is None:
            raise HTTPException(status_code=404, detail="unknown operation")
        return op

    @api.get("/exits/{exit_id}/history")
    async def history(
        exit_id: str, limit: Annotated[int, Query(ge=1, le=1000)] = 100
    ) -> list[dict]:
        hub.driver(exit_id)
        return hub.store.history(exit_id, limit)

    @api.get("/sessions")
    async def list_sessions() -> list[dict]:
        return hub.describe_sessions()

    @api.delete("/sessions/{session_id}", status_code=204)
    async def release_session(session_id: str) -> Response:
        if not hub.release_by_id(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return Response(status_code=204)

    app.include_router(api)
    return app
