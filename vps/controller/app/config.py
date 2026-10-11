import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

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


class VpnExitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    type: Literal["vpn"]
    proxy_port: int
    proxy_url: str
    gluetun_control_url: str
    cooldown_seconds: float = 30
    max_attempts: int = Field(default=3, ge=1)
    ready_timeout_seconds: float = 90
    poll_seconds: float = 3
    rotate_every_sessions: int | None = Field(default=None, ge=1)
    session_ttl_seconds: float = Field(default=900, gt=0)
    recover_after_seconds: float | None = Field(default=120, gt=0)
    recover_interval_seconds: float = Field(default=300, gt=0)


class ExitsFile(BaseModel):
    exits: list[VpnExitConfig]


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if len(env["API_TOKEN"]) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"API_TOKEN must be at least {MIN_TOKEN_LENGTH} characters")
    optional = {field: env[name] for name, field in OPTIONAL_ENV.items() if env.get(name)}
    try:
        return Settings(
            api_token=env["API_TOKEN"],
            proxy_user=env["PROXY_USER"],
            proxy_password=env["PROXY_PASSWORD"],
            gluetun_api_key=env["GLUETUN_API_KEY"],
            **optional,
        )
    except ValidationError as e:
        raise ConfigError(f"invalid settings: {e}") from e


def load_exits(path: Path) -> list[VpnExitConfig]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        exits = ExitsFile.model_validate(data).exits
    except (OSError, yaml.YAMLError, ValidationError) as e:
        raise ConfigError(f"cannot load {path}: {e}") from e
    ids = [exit.id for exit in exits]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"duplicate exit id in {path}")
    return exits
