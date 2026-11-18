from typing import Protocol

from .config import ExitConfig


class ExitError(Exception):
    pass


class CountryRejected(ExitError):
    """Pays que cette sortie ne peut pas donner."""


class ExitDriver(Protocol):
    """Ce que le hub attend d'une sortie, quel que soit son type."""

    config: ExitConfig

    async def measure_ip(self) -> str: ...

    async def country(self) -> str | None: ...

    async def reconnect(self, countries: list[str] | None = None) -> None: ...

    async def aclose(self) -> None: ...
