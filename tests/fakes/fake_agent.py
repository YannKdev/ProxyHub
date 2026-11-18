import httpx


class FakeAgent:
    """Imite un téléphone joint par le tunnel : son agent (/status, /rotate) et son proxy.

    `ips` : IP de sortie successives, une nouvelle à chaque rotation acceptée.
    `rotate_status` : code renvoyé par /rotate (202, ou 501 si la rotation n'est pas gérée).
    """

    def __init__(self, ips: list[str], rotate_status: int = 202, sim_country: str | None = "fr"):
        self.rotate_status = rotate_status
        self.sim_country = sim_country
        self.reachable = True
        self.requests: list[tuple[str, str]] = []
        self._ips = list(ips)
        self.ip = self._ips.pop(0)

    def agent(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("tunnel down", request=request)
        route = (request.method, request.url.path)
        self.requests.append(route)
        if route == ("GET", "/status"):
            phone = {"root": True, "sim_country": self.sim_country}
            return httpx.Response(200, json={"online": True, "ip": self.ip, "phone": phone})
        if route == ("POST", "/rotate"):
            if self.rotate_status == 202 and self._ips:
                self.ip = self._ips.pop(0)
            return httpx.Response(self.rotate_status, json={})
        return httpx.Response(404)

    def proxy(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("tunnel down", request=request)
        return httpx.Response(200, text=self.ip)

    def clients(self) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        agent = httpx.AsyncClient(
            base_url="http://tunnel:19000", transport=httpx.MockTransport(self.agent)
        )
        web = httpx.AsyncClient(transport=httpx.MockTransport(self.proxy))
        return agent, web
