import httpx


class FakeAgent:
    """Imite un téléphone joint par le tunnel : son agent (/status, /rotate) et son proxy.

    `ips` : IP de sortie successives, une nouvelle à chaque rotation réussie. Quand la liste est
    épuisée, l'IP ne change plus.
    `rotate_status` : code renvoyé par /rotate (202, 409, 501, 503).
    Après un /rotate accepté, le tunnel tombe pendant `down_requests` requêtes, puis la rotation
    se termine (en échec si `rotation_error` est défini).
    """

    def __init__(
        self,
        ips: list[str],
        rotate_status: int = 202,
        sim_country: str | None = "fr",
        down_requests: int = 1,
        rotation_error: str | None = None,
    ):
        self.rotate_status = rotate_status
        self.sim_country = sim_country
        self.down_requests = down_requests
        self.rotation_error = rotation_error
        self.reachable = True
        self.requests: list[tuple[str, str]] = []
        self.rotation: dict | None = None
        self.rotations = 0
        self._down = 0
        self._ips = list(ips)
        self.ip = self._ips.pop(0)

    def _through_tunnel(self, request: httpx.Request) -> None:
        if not self.reachable:
            raise httpx.ConnectError("tunnel down", request=request)
        if self._down:
            self._down -= 1
            raise httpx.ConnectError("tunnel down during rotation", request=request)
        if self.rotation and self.rotation["status"] == "running":
            if self.rotation_error:
                self.rotation.update(status="failed", error=self.rotation_error)
            else:
                if self._ips:
                    self.ip = self._ips.pop(0)
                self.rotation.update(status="done", new_ip=self.ip)

    def agent(self, request: httpx.Request) -> httpx.Response:
        self._through_tunnel(request)
        route = (request.method, request.url.path)
        self.requests.append(route)
        if route == ("GET", "/status"):
            phone = {"root": True, "sim_country": self.sim_country}
            rotation = {"engine": "airplane", "last": self.rotation}
            return httpx.Response(
                200, json={"online": True, "ip": self.ip, "phone": phone, "rotation": rotation}
            )
        if route == ("POST", "/rotate"):
            if self.rotate_status != 202:
                return httpx.Response(self.rotate_status, json={"detail": "refused"})
            self.rotations += 1
            self.rotation = {"id": f"r{self.rotations}", "status": "running", "error": None}
            self._down = self.down_requests
            return httpx.Response(202, json={"id": self.rotation["id"]})
        return httpx.Response(404)

    def proxy(self, request: httpx.Request) -> httpx.Response:
        self._through_tunnel(request)
        return httpx.Response(200, text=self.ip)

    def clients(self) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        agent = httpx.AsyncClient(
            base_url="http://tunnel:19000", transport=httpx.MockTransport(self.agent)
        )
        web = httpx.AsyncClient(transport=httpx.MockTransport(self.proxy))
        return agent, web
