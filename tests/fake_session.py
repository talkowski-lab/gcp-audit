"""The fake session that tests install.

It subclasses `guard.GuardedSession` and overrides only `_dispatch`, the one method that touches
the network. Every request still passes the real admission checks, so an unknown method, query key
or parameter raises just as it would in a real run.
"""
from __future__ import annotations

from typing import Any

from gcp_audit import guard


class FakeReply:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload, self.status_code = payload, status
        self.reason = "OK" if status < 400 else "error"

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession(guard.GuardedSession):
    """Answers each call from `routes[method_id]`.

    A route is a dict, or a list of pages consumed in call order across the whole run, not per
    URL. A run that calls one method for two resources must stage both resources' pages in order.
    When the pages run out, the error names the URL and page number.

    `failures` makes a call raise `guard.ReadFailed`, to exercise the gap path. Its key is a method
    id, or a `(method_id, url-substring)` pair when only one resource's read should fail.
    A route key may also be such a pair.
    """

    def __init__(self, routes: dict[str, Any] | None = None,
                 failures: dict[Any, tuple[int, str]] | None = None) -> None:
        super().__init__(inner=None)
        self.routes: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v)
                                       for k, v in (routes or {}).items()}
        self.failures: dict[Any, tuple[int, str]] = dict(failures or {})
        self.consumed: dict[str, int] = {}
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.dispatch_kwargs: list[dict] = []

    def _failure(self, method_id: str, url: str) -> tuple[int, str] | None:
        """The staged failure for this call. A URL-specific entry wins over a method-wide one."""
        for key, answer in self.failures.items():
            if isinstance(key, tuple) and key[0] == method_id and key[1] in url:
                return answer
        return self.failures.get(method_id)

    def _dispatch(self, method_id: str, verb: str, url: str, query, body) -> dict[str, Any]:
        self.calls.append((method_id, url, query, body))
        failure = self._failure(method_id, url)
        if failure is not None:
            raise guard.ReadFailed(method_id, failure[0], failure[1])
        route = next((answer for key, answer in self.routes.items()
                      if isinstance(key, tuple) and key[0] == method_id and key[1] in url),
                     self.routes.get(method_id))
        if route is None:
            raise AssertionError(f"no route staged for {method_id}; calls so far: {self.calls}")
        if isinstance(route, list):
            index = self.consumed.get(method_id, 0)
            self.consumed[method_id] = index + 1
            if index >= len(route):
                raise AssertionError(
                    f"{method_id} at {url} asked for page {index + 1} of {len(route)} staged; "
                    f"pages are consumed in call order across the run, so stage every resource's "
                    f"pages, in order (this method has been called {index + 1} times so far)")
            return route[index]
        return route

    # -- helpers tests assert on -------------------------------------------------------------

    def urls(self, method_id: str) -> list[str]:
        return [url for mid, url, _q, _b in self.calls if mid == method_id]

    def queries(self, method_id: str) -> list[dict | None]:
        return [q for mid, _u, q, _b in self.calls if mid == method_id]

    def count(self, method_id: str) -> int:
        return sum(1 for mid, *_ in self.calls if mid == method_id)


def install(routes: dict[str, Any] | None = None,
            failures: dict[Any, tuple[int, str]] | None = None) -> FakeSession:
    """Reset the guard's state and install a fake session."""
    guard.reset()
    fake = FakeSession(routes, failures)
    guard.install(fake)
    return fake
