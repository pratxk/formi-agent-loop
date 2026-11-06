"""Thin HTTP client for the simulator. Retries transient failures; 4xx are returned to the caller."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger("controller.api")


class Sim:
    def __init__(self, url: str, timeout: float = 120.0) -> None:
        self.http = httpx.Client(base_url=url, timeout=timeout)

    def call(self, method: str, path: str, *, retry: bool = True, **kw: Any) -> tuple[int, Any]:
        """Return (status, json). Retries connection errors and 5xx with backoff when `retry`."""
        tries = 6 if retry else 1
        for attempt in range(tries):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError as e:
                log.warning("transport error %s %s: %s", method, path, e)
            else:
                if r.status_code < 500 or attempt == tries - 1:
                    return r.status_code, _json(r)
                log.warning("%s %s -> %s", method, path, r.status_code)
            time.sleep(min(0.5 * 2**attempt, 8))
        return 599, {"error": "unreachable"}

    def get(self, path: str, **kw: Any) -> Any:
        status, body = self.call("GET", path, **kw)
        if status != 200:
            raise RuntimeError(f"GET {path} -> {status} {body}")
        return body

    def chunk_lines(self, n: int) -> list[str]:
        for attempt in range(6):
            try:
                r = self.http.get(f"/events/chunks/{n}")
                if r.status_code == 200:
                    return r.text.splitlines()
            except httpx.TransportError:
                pass
            time.sleep(min(0.5 * 2**attempt, 8))
        raise RuntimeError(f"chunk {n} unreadable")


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"error": "non_json", "detail": r.text[:200]}
