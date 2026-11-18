import ipaddress
from typing import Any

import httpx

from .config import MobileExitConfig, Settings
from .countries import country_names, in_country
from .driver import CountryRejected, ExitError


class MobileExit:
    """Sortie par un téléphone en 4G : son proxy et son agent passent par le tunnel SSH."""

    def __init__(
        self,
        config: MobileExitConfig,
        agent: httpx.AsyncClient,
        web: httpx.AsyncClient,
        ip_check_url: str,
    ):
        self.config = config
        self.agent = agent
        self.web = web
        self.ip_check_url = ip_check_url
        # Pays de la SIM : imposé par la configuration, sinon lu une fois sur le téléphone.
        self.names = country_names(config.country) if config.country else None

    async def measure_ip(self) -> str:
        response = await self.web.get(self.ip_check_url)
        response.raise_for_status()
        return str(ipaddress.ip_address(response.text.strip()))

    async def country(self) -> str | None:
        if self.names is None:
            code = (await self.status()).get("phone", {}).get("sim_country")
            if code:
                self.names = country_names(code)
        return self.names[0] if self.names else None

    async def reconnect(self, countries: list[str] | None = None) -> None:
        if countries:
            current = await self.country()
            if not self.names or not any(in_country(n, countries) for n in self.names):
                where = current or "an unknown country"
                raise CountryRejected(f"mobile exit is in {where}, not {countries[0]}")
        response = await self.agent.post("/rotate")
        if response.status_code == 501:
            raise ExitError("rotation not available on this phone")
        if response.status_code == 409:
            raise ExitError("phone busy with another operation")
        if response.status_code != 202:
            raise ExitError(f"phone agent answered {response.status_code}")

    async def status(self) -> dict[str, Any]:
        response = await self.agent.get("/status")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self.agent.aclose()
        await self.web.aclose()


def make_mobile_exit(config: MobileExitConfig, settings: Settings) -> MobileExit:
    agent = httpx.AsyncClient(base_url=config.agent_url, timeout=15, trust_env=False)
    web = httpx.AsyncClient(proxy=config.proxy_url, timeout=15, trust_env=False)
    return MobileExit(config, agent, web, settings.ip_check_url)
