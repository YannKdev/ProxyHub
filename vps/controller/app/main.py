import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ConfigError, Settings, load_exits, load_settings
from .hub import (
    CooldownActive,
    CountryMismatch,
    ExitBusy,
    ExitDown,
    ExitInUse,
    Hub,
    UnknownExit,
)
from .store import Store
from .vpn import VpnExit, make_vpn_exit

log = logging.getLogger("proxyhub")

Country = Annotated[str | None, Query(pattern=r"^[A-Za-z][A-Za-z .'-]{1,39}$")]


def create_app(
    settings: Settings | None = None, exits: dict[str, VpnExit] | None = None
) -> FastAPI:
    if settings is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = settings or load_settings()
        if exits is None:
            exits = {c.id: make_vpn_exit(c, settings) for c in load_exits(settings.exits_file)}
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1) from None

    hub = Hub(settings, exits, Store(settings.db_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.startup()
        loops = []
        if settings.monitor_seconds > 0:
            loops.append(asyncio.create_task(hub.monitor()))
        if settings.sweep_seconds > 0:
            loops.append(asyncio.create_task(hub.sweeper()))
        yield
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

    @app.exception_handler(UnknownExit)
    async def unknown_exit(request: Request, exc: UnknownExit) -> JSONResponse:
        return error(404, "unknown exit")

    @app.exception_handler(ExitBusy)
    async def exit_busy(request: Request, exc: ExitBusy) -> JSONResponse:
        return error(
            409, "an operation is already running on this exit", operation_id=exc.operation_id
        )

    @app.exception_handler(ExitInUse)
    async def exit_in_use(request: Request, exc: ExitInUse) -> JSONResponse:
        return error(
            409,
            f"exit has {exc.active_sessions} active sessions, use force=true to rotate anyway",
            active_sessions=exc.active_sessions,
        )

    @app.exception_handler(CountryMismatch)
    async def country_mismatch(request: Request, exc: CountryMismatch) -> JSONResponse:
        return error(
            409, f"exit is in {exc.current}, rotate with ?country= first", country=exc.current
        )

    @app.exception_handler(ExitDown)
    async def exit_down(request: Request, exc: ExitDown) -> JSONResponse:
        return error(503, "exit is down")

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

    @api.post("/exits/{exit_id}/sessions", status_code=201)
    async def open_session(request: Request, exit_id: str, country: Country = None) -> dict:
        session = await hub.open_session(exit_id, country)
        session["proxy_url"] = f"http://{request.url.hostname}:{session['proxy_port']}"
        return session

    @api.delete("/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str) -> Response:
        if hub.close_session(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return Response(status_code=204)

    app.include_router(api)
    return app
