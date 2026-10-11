import ipaddress

import httpx

from .config import Settings, VpnExitConfig


class ExitError(Exception):
    pass


class VpnExit:
    def __init__(
        self,
        config: VpnExitConfig,
        control: httpx.AsyncClient,
        web: httpx.AsyncClient,
        ip_check_url: str,
    ):
        self.config = config
        self.control = control
        self.web = web
        self.ip_check_url = ip_check_url

    async def measure_ip(self) -> str:
        response = await self.web.get(self.ip_check_url)
        response.raise_for_status()
        return str(ipaddress.ip_address(response.text.strip()))

    async def country(self) -> str | None:
        response = await self.control.get("/v1/vpn/settings")
        response.raise_for_status()
        countries = response.json()["provider"]["server_selection"]["countries"] or []
        return countries[0] if countries else None

    async def reconnect(self, country: str | None = None) -> None:
        if country:
            response = await self.control.put(
                "/v1/vpn/settings",
                json={"provider": {"server_selection": {"countries": [country]}}},
            )
            if response.status_code == 400:
                raise ExitError(f"gluetun rejected country {country!r}")
            response.raise_for_status()
            # Gluetun relance lui-même le VPN quand les réglages changent.
            if "unchanged" not in response.text:
                return
        for status in ("stopped", "running"):
            response = await self.control.put("/v1/vpn/status", json={"status": status})
            response.raise_for_status()

    async def aclose(self) -> None:
        await self.control.aclose()
        await self.web.aclose()


def make_vpn_exit(config: VpnExitConfig, settings: Settings) -> VpnExit:
    proxy = httpx.URL(config.proxy_url).copy_with(
        username=settings.proxy_user, password=settings.proxy_password
    )
    control = httpx.AsyncClient(
        base_url=config.gluetun_control_url,
        headers={"X-API-Key": settings.gluetun_api_key},
        timeout=60,
        trust_env=False,
    )
    web = httpx.AsyncClient(proxy=proxy, timeout=15, trust_env=False)
    return VpnExit(config, control, web, settings.ip_check_url)
