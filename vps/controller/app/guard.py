import logging
import math
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger("proxyhub")


class Guard:
    """Blocage temporaire d'une IP après trop d'échecs d'authentification."""

    def __init__(
        self,
        max_failures: int,
        failure_window_seconds: float,
        block_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.failure_window_seconds = failure_window_seconds
        self.block_seconds = block_seconds
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked_for(self, ip: str) -> int | None:
        """Secondes de blocage restantes pour cette IP, ou None."""
        until = self._blocked_until.get(ip)
        if until is None:
            return None
        left = until - self.clock()
        if left <= 0:
            del self._blocked_until[ip]
            return None
        return math.ceil(left)

    def auth_failed(self, ip: str) -> None:
        now = self.clock()
        self._forget_old(now)
        failures = self._failures.setdefault(ip, deque())
        failures.append(now)
        if len(failures) >= self.max_failures:
            self._blocked_until[ip] = now + self.block_seconds
            del self._failures[ip]
            log.warning(
                "ip blocked for %ss after failed authentications ip=%s", self.block_seconds, ip
            )

    def _forget_old(self, now: float) -> None:
        before = now - self.failure_window_seconds
        for ip in list(self._failures):
            failures = self._failures[ip]
            while failures and failures[0] < before:
                failures.popleft()
            if not failures:
                del self._failures[ip]
