import os
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MIN_TOKEN_LENGTH = 32
REQUIRED_ENV = ("API_TOKEN", "PROXY_USER", "PROXY_PASSWORD", "GLUETUN_API_KEY")
OPTIONAL_ENV = {
    "IP_CHECK_URL": "ip_check_url",
    "EXITS_FILE": "exits_file",
    "DB_PATH": "db_path",
    "MONITOR_SECONDS": "monitor_seconds",
    "SWEEP_SECONDS": "sweep_seconds",
    "HISTORY_DAYS": "history_days",
    "AUTH_MAX_FAILURES": "auth_max_failures",
    "AUTH_FAILURE_WINDOW_SECONDS": "auth_failure_window_seconds",
    "AUTH_BLOCK_SECONDS": "auth_block_seconds",
    "PROXY_PORT": "proxy_port",
    "SESSION_IDLE_SECONDS": "session_idle_seconds",
    "MAX_HOLD_MINUTES": "max_hold_minutes",
    "PROXY_WAIT_SECONDS": "proxy_wait_seconds",
}


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    api_token: str
    proxy_user: str
    proxy_password: str
    gluetun_api_key: str
    ip_check_url: str = "https://api.ipify.org"
    exits_file: Path = Path("/config/exits.yaml")
    db_path: Path = Path("/data/proxyhub.db")
    monitor_seconds: float = 60
    sweep_seconds: float = 5
    history_days: int = 7
    auth_max_failures: int = Field(default=5, ge=1)
    auth_failure_window_seconds: float = Field(default=600, gt=0)
    auth_block_seconds: float = Field(default=1800, gt=0)
    compose_profiles: list[str] = Field(default_factory=list)
    # Proxy d'entrée. None : pas de proxy (tests de l'API seule).
    proxy_port: int | None = 8080
    proxy_connect_ports: list[int] = Field(default_factory=lambda: [80, 443])
    session_idle_seconds: float = Field(default=300, gt=0)
    max_hold_minutes: int = Field(default=1440, ge=1)
    # Attente maximale d'un client pendant qu'on prépare sa sortie (rotation, changement de pays).
    proxy_wait_seconds: float = Field(default=150, gt=0)


class ExitConfig(BaseModel):
    """Réglages communs à toutes les sorties."""

    model_config = ConfigDict(extra="forbid")
    # Le proxy d'entrée envoie PROXY_USER/PROXY_PASSWORD au proxy de la sortie.
    proxy_credentials: ClassVar[bool] = True
    # reconnect() rend la main une fois la nouvelle IP en place : la première IP mesurée
    # ensuite est la bonne. Sinon (Gluetun), on mesure jusqu'à voir une autre IP.
    reconnect_settles: ClassVar[bool] = False

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    # Groupe choisi par -pool- dans le nom d'utilisateur du proxy (fournisseur).
    pool: str = Field(pattern=r"^[a-z0-9]+$")
    profile: str | None = None
    # Port publié de la sortie seule, None si elle n'en a pas.
    proxy_port: int | None = None
    proxy_url: str
    cooldown_seconds: float = 30
    max_attempts: int = Field(default=3, ge=1)
    ready_timeout_seconds: float = 90
    poll_seconds: float = 3
    recover_after_seconds: float | None = Field(default=120, gt=0)
    recover_interval_seconds: float = Field(default=300, gt=0)


class VpnExitConfig(ExitConfig):
    type: Literal["vpn"]
    proxy_port: int
    gluetun_control_url: str


class MobileExitConfig(ExitConfig):
    """Téléphone joint par le tunnel SSH qu'il ouvre lui-même vers le VPS."""

    # Le proxy du téléphone n'a pas d'authentification et transmettrait l'en-tête au site.
    proxy_credentials: ClassVar[bool] = False
    # reconnect() attend la fin de la rotation sur le téléphone.
    reconnect_settles: ClassVar[bool] = True

    type: Literal["mobile"]
    pool: str = Field(default="mobile", pattern=r"^[a-z0-9]+$")
    agent_url: str
    # La sortie ne peut pas changer de pays : celui de la SIM, lu par l'agent. Un nom ou un
    # code ISO ici l'impose.
    country: str | None = None
    cooldown_seconds: float = 60
    ready_timeout_seconds: float = 180
    poll_seconds: float = 5
    # Le téléphone se répare lui-même : le VPS ne le joint plus quand la 4G est coupée.
    recover_after_seconds: float | None = Field(default=None, gt=0)


AnyExitConfig = Annotated[VpnExitConfig | MobileExitConfig, Field(discriminator="type")]


class ExitsFile(BaseModel):
    exits: list[AnyExitConfig]


def split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if len(env["API_TOKEN"]) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
    optional = {field: env[name] for name, field in OPTIONAL_ENV.items() if env.get(name)}
    profiles = split_list(env.get("COMPOSE_PROFILES", ""))
    ports = split_list(env.get("PROXY_CONNECT_PORTS", ""))
    if ports:
        optional["proxy_connect_ports"] = ports
    try:
        return Settings(
            api_token=env["API_TOKEN"],
            proxy_user=env["PROXY_USER"],
            proxy_password=env["PROXY_PASSWORD"],
            gluetun_api_key=env["GLUETUN_API_KEY"],
            compose_profiles=profiles,
            **optional,
        )
    except ValidationError as e:
        raise ConfigError(f"invalid settings: {e}") from e


def load_exits(path: Path, profiles: Collection[str] = ()) -> list[AnyExitConfig]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        exits = ExitsFile.model_validate(data).exits
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"cannot load {path}: {e}") from e
    ids = [exit.id for exit in exits]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"duplicate exit id in {path}")
    return [exit for exit in exits if exit.profile is None or exit.profile in profiles]
