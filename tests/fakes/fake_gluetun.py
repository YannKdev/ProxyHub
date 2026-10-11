import json

import httpx

KNOWN_COUNTRIES = {"france", "belgium", "germany"}


class FakeGluetun:
    """Imite le serveur de contrôle et le proxy de Gluetun.

    `ips` : IP de sortie successives, une nouvelle à chaque reconnexion.
    Quand la liste est épuisée, l'IP ne change plus.
    """

    def __init__(self, ips: list[str], country: str = "france", api_key: str = "gluetun-key"):
        self.api_key = api_key
        self.countries = [country]
        self.status = "running"
        self.reachable = True
        self.connects = True
        self.requests: list[tuple[str, str]] = []
        self._ips = list(ips)
        self.ip = self._ips.pop(0)

    def _reconnect(self) -> None:
        if not self.connects:
            self.status = "stopped"
            return
        if self._ips:
            self.ip = self._ips.pop(0)
        self.status = "running"

    def control(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("gluetun unreachable", request=request)
        if request.headers.get("X-API-Key") != self.api_key:
            return httpx.Response(401)
        route = (request.method, request.url.path)
        self.requests.append(route)

        if route == ("GET", "/v1/vpn/settings"):
            return httpx.Response(
                200, json={"provider": {"server_selection": {"countries": self.countries}}}
            )
        if route == ("PUT", "/v1/vpn/settings"):
            countries = json.loads(request.content)["provider"]["server_selection"]["countries"]
            if any(c.casefold() not in KNOWN_COUNTRIES for c in countries):
                return httpx.Response(400, text="invalid country")
            if countries == self.countries:
                return httpx.Response(200, text="settings left unchanged")
            self.countries = countries
            self._reconnect()
            return httpx.Response(200, text=self.status)
        if route == ("PUT", "/v1/vpn/status"):
            wanted = json.loads(request.content)["status"]
            if wanted == "running":
                self._reconnect()
            else:
                self.status = "stopped"
            return httpx.Response(200, text=wanted)
        return httpx.Response(404)

    def proxy(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable or self.status != "running":
            raise httpx.ConnectError("proxy unreachable", request=request)
        return httpx.Response(200, text=self.ip)

    def clients(self) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        control = httpx.AsyncClient(
            base_url="http://gluetun:8000",
            headers={"X-API-Key": self.api_key},
            transport=httpx.MockTransport(self.control),
        )
        web = httpx.AsyncClient(transport=httpx.MockTransport(self.proxy))
        return control, web
