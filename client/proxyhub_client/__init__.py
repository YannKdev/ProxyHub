import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlparse

import requests

__all__ = ["ProxyHub", "ProxyHubError"]

log = logging.getLogger("proxyhub_client")


class ProxyHubError(Exception):
    def __init__(self, message: str, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


class ProxyHub:
    def __init__(
        self,
        base_url: str,
        token: str,
        proxy_user: str,
        proxy_password: str,
        timeout: float = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname
        self.timeout = timeout
        self._credentials = f"{quote(proxy_user, safe='')}:{quote(proxy_password, safe='')}"
        self._api = requests.Session()
        self._api.trust_env = False
        self._api.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self._api.request(method, self.base_url + path, timeout=self.timeout, **kwargs)
        if response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = {"detail": response.text}
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

    def operation(self, operation_id: str) -> dict:
        return self._request("GET", f"/operations/{operation_id}")

    def proxy_url(self, port: int) -> str:
        return f"http://{self._credentials}@{self.host}:{port}"

    def rotate(
        self,
        exit_id: str,
        country: str | None = None,
        force: bool = False,
        wait: bool = True,
        timeout: float = 600,
    ) -> dict | str:
        """Nouvelle IP. Renvoie le résultat de la rotation, ou son identifiant si wait=False."""
        params: dict[str, str] = {}
        if country:
            params["country"] = country
        if force:
            params["force"] = "true"
        op = self._request("POST", f"/exits/{exit_id}/rotate", params=params)
        if not wait:
            return op["operation_id"]
        return self.wait(op["operation_id"], timeout)

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

    @contextmanager
    def session(self, exit_id: str, country: str | None = None) -> Iterator[requests.Session]:
        """requests.Session qui sort par `exit_id`. Infos de la session dans `.proxyhub`."""
        info = self._open_session(exit_id, country)
        proxy = self.proxy_url(info["proxy_port"])
        http = requests.Session()
        http.trust_env = False
        http.proxies = {"http": proxy, "https": proxy}
        http.proxyhub = info
        try:
            yield http
        finally:
            http.close()
            try:
                self._request("DELETE", f"/sessions/{info['session_id']}")
            except (ProxyHubError, requests.RequestException) as e:
                log.warning("could not close session %s: %s", info["session_id"], e)

    def _open_session(self, exit_id: str, country: str | None, timeout: float = 600) -> dict:
        params = {"country": country} if country else {}
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._request("POST", f"/exits/{exit_id}/sessions", params=params)
            except ProxyHubError as e:
                if e.status != 409 or time.monotonic() > deadline:
                    raise
                if e.data.get("operation_id"):
                    # Rotation en cours : on attend sa fin.
                    self._wait_quietly(e.data["operation_id"], timeout)
                elif e.data.get("country"):
                    # Sortie dans un autre pays : on la fait changer de pays.
                    self.rotate(exit_id, country=country, timeout=timeout)
                else:
                    raise

    def _wait_quietly(self, operation_id: str, timeout: float) -> None:
        try:
            self.wait(operation_id, timeout)
        except ProxyHubError as e:
            log.warning("%s", e)
