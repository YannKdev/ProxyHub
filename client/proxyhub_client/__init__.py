import re
import time
from typing import Any
from urllib.parse import quote, urlparse

import requests

__all__ = ["ProxyHub", "ProxyHubError"]

SESSION_PATTERN = re.compile(r"[A-Za-z0-9_]{1,64}")


class ProxyHubError(Exception):
    def __init__(self, message: str, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


class ProxyHub:
    """Proxy d'entrée (`proxy_url`, `proxy`) et API d'administration (avec `token`)."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        proxy_user: str | None = None,
        proxy_password: str | None = None,
        proxy_port: int = 8080,
        timeout: float = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname
        self.proxy_user = proxy_user
        self.proxy_password = proxy_password
        self.proxy_port = proxy_port
        self.timeout = timeout
        self._api = requests.Session()
        self._api.trust_env = False
        if token:
            self._api.headers["Authorization"] = f"Bearer {token}"

    # Proxy d'entrée

    def proxy_url(
        self,
        session: str | int | None = None,
        pool: str | None = None,
        country: str | None = None,
        hold: int | None = None,
        ip: int | None = None,
    ) -> str:
        """URL du proxy d'entrée. Même `session` = même sortie, donc même IP ; nouvelle
        `session` = autre IP. Sans `session`, le VPS garde une session par IP de client.

        pool : nordvpn, cyberghost, mobile ou vpn (défaut). country : code à deux lettres.
        hold : minutes pendant lesquelles la sortie reste réservée sans trafic.
        ip : numéro d'IP de la session ; le passer à un nombre plus grand libère la sortie
        et donne une IP neuve (la session ne tient jamais plus d'une sortie).
        """
        if not self.proxy_user or not self.proxy_password:
            raise ValueError("proxy_user and proxy_password are needed to use the proxy")
        options: list[str] = []
        if pool:
            options += ["pool", pool]
        if country:
            options += ["country", country.lower()]
        if session is not None:
            if not SESSION_PATTERN.fullmatch(str(session)):
                raise ValueError("session must be 1 to 64 letters, digits or underscores")
            options += ["session", str(session)]
        if ip is not None:
            if int(ip) < 1:
                raise ValueError("ip must be a positive number")
            options += ["ip", str(int(ip))]
        if hold:
            options += ["hold", str(int(hold))]
        user = quote("-".join([self.proxy_user, *options]), safe="")
        return f"http://{user}:{quote(self.proxy_password, safe='')}@{self.host}:{self.proxy_port}"

    def proxy(
        self,
        session: str | int | None = None,
        pool: str | None = None,
        country: str | None = None,
        hold: int | None = None,
        ip: int | None = None,
    ) -> requests.Session:
        """requests.Session qui passe par le proxy d'entrée (voir `proxy_url`)."""
        url = self.proxy_url(session, pool, country, hold, ip)
        http = requests.Session()
        http.trust_env = False
        http.proxies = {"http": url, "https": url}
        return http

    # Administration (API)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self._api.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = {"detail": response.text}
            if "Retry-After" in response.headers:
                data["retry_after"] = int(response.headers["Retry-After"])
            raise ProxyHubError(
                f"{method} {path}: {response.status_code} {data.get('detail')}",
                response.status_code,
                data,
            )
        return response.json() if response.content else None

    def exits(self) -> list[dict]:
        return self._request("GET", "/exits")

    def ip(self, exit_id: str) -> str:
        return self._request("GET", f"/exits/{exit_id}/ip")["ip"]

    def history(self, exit_id: str, limit: int = 100) -> list[dict]:
        return self._request("GET", f"/exits/{exit_id}/history", params={"limit": limit})

    def sessions(self) -> list[dict]:
        return self._request("GET", "/sessions")

    def release(self, session_id: str) -> None:
        self._request("DELETE", f"/sessions/{session_id}")

    def operation(self, operation_id: str) -> dict:
        return self._request("GET", f"/operations/{operation_id}")

    def rotate(
        self,
        exit_id: str,
        country: str | None = None,
        force: bool = False,
        wait: bool = True,
        timeout: float = 600,
    ) -> dict | str:
        """Nouvelle IP pour une sortie. Renvoie le résultat, ou l'identifiant si wait=False.

        Avec wait=True, un délai minimal en cours (429) est attendu puis la demande est refaite.
        """
        params: dict[str, str] = {}
        if country:
            params["country"] = country
        if force:
            params["force"] = "true"
        deadline = time.monotonic() + timeout
        while True:
            try:
                op = self._request("POST", f"/exits/{exit_id}/rotate", params=params)
                break
            except ProxyHubError as e:
                retry_after = e.data.get("retry_after")
                if not wait or retry_after is None or time.monotonic() + retry_after > deadline:
                    raise
                time.sleep(retry_after)
        if not wait:
            return op["operation_id"]
        return self.wait(op["operation_id"], max(deadline - time.monotonic(), 1))

    def wait(self, operation_id: str, timeout: float = 600, poll: float = 2) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            op = self.operation(operation_id)
            if op["status"] == "done":
                return op["result"]
            if op["status"] == "failed":
                raise ProxyHubError(f"operation {operation_id} failed: {op['error']}", data=op)
            if time.monotonic() > deadline:
                raise ProxyHubError(f"operation {operation_id} still running after {timeout} s")
            time.sleep(poll)
