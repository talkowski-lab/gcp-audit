"""guard.py — the read-only guarantee, and the only thing in this package that can reach a wire.

See `docs/SAFETY.md`. Enforced, in order, on every request:

1. **Scopes, fail-closed.** Granted scopes are read off `tokeninfo` (the server's answer), never
   `creds.scopes` (a client-side claim). An empty, unparseable or over-wide set refuses.
2. **Method allowlist.** Every request is a named read in `methods.py`; callers name a method and
   parameters and never build a URL, and `match()` re-checks the URL that was built.
3. **No redirects, bounded retries, a request cap**, so a paging bug stops at a cap, not a bill.

Not checked: no IAM policy is read and nothing calls `*.testIamPermissions`. Mutations are stopped
by construction, not by verifying that the credential could not write.
"""
from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

# The only file allowed to import `requests` (lint-enforced); used for the `tokeninfo` GET, which
# cannot go through the session it verifies.
import google.auth
import requests
from google.auth import exceptions as auth_exceptions
from google.auth.transport.requests import AuthorizedSession, Request

from gcp_audit import methods

#: The scopes a pass asks for and runs with (evidence: `docs/API-EVIDENCE.md`). An explicit list
#: because `cloud-platform.read-only` alone is refused by compute and monitoring; BigQuery has no
#: read-only scope of its own and rides `cloud-platform.read-only`.
NARROW_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform.read-only",
    "https://www.googleapis.com/auth/devstorage.read_only",
    "https://www.googleapis.com/auth/compute.readonly",
    "https://www.googleapis.com/auth/monitoring.read",
    "https://www.googleapis.com/auth/cloud-billing.readonly",
)

#: What a run may hold. A token with `cloud-platform`, or any scope not above, is refused; there
#: is no flag to widen it.
SCOPE_ALLOWLIST: frozenset[str] = frozenset(NARROW_SCOPES)

TOKENINFO_HOST = "oauth2.googleapis.com"
TOKENINFO_PATH = "/tokeninfo"

#: Enough for a lab estate: roughly one page per read plus a margin, per process.
MAX_REQUESTS = 6000
RETRY_STATUS = (429, 500, 502, 503, 504)
BACKOFF_SECONDS = (1, 3)

TIMEOUT_SECONDS = 30


class AuditError(RuntimeError):
    """Anything this package refuses to paper over."""


class ScopeRefusal(AuditError):
    """The granted scope set is unknown, empty, or wider than the allowlist. REFUSE."""


class ReadFailed(AuditError):
    """The cloud said no. Carries what it said, so a gap in the table can name its cause."""

    def __init__(self, method_id: str, status: int | None, reason: str) -> None:
        super().__init__(f"{method_id}: HTTP {status} — {reason}")
        self.method_id, self.status, self.reason = method_id, status, reason


_STATE: dict[str, Any] = {
    "state": "REFUSED",
    "granted": [],
    "requested": list(NARROW_SCOPES),
    "verified_via": "none",
    "reason": "no session has been created, so the granted scope set is unverified",
    "principal": None,
    "principal_verified": False,
}
_STATS: dict[str, int] = {"requests": 0, "pages": 0, "retries": 0}
_SESSION: Any = None


# ------------------------------------------------------------------- layer 1

def _is_tokeninfo(verb: str, url: str) -> bool:
    """The one pre-allowlist request: reading our own token's grant."""
    return verb == "GET" and url.startswith(f"https://{TOKENINFO_HOST}{TOKENINFO_PATH}")


def _granted_scopes(token: str) -> list[str]:
    """Ask the server what the token holds. Never `creds.scopes`: a claim is not a grant."""
    if not token:
        raise ScopeRefusal("no access token to introspect, so the granted scope set is unknown")
    try:
        reply = requests.get(f"https://{TOKENINFO_HOST}{TOKENINFO_PATH}",
                             params={"access_token": token}, timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        raise ScopeRefusal(f"cannot verify the granted scope set: {type(exc).__name__}: {exc}"
                           ) from exc
    if reply.status_code != 200:
        raise ScopeRefusal(f"token-introspection answered {reply.status_code}, so the granted "
                           "scope set is unknown; refusing rather than assuming")
    try:
        payload = reply.json()
    except ValueError as exc:
        raise ScopeRefusal("token-introspection answered something that is not JSON") from exc
    scope_text = payload.get("scope") or ""
    granted = [s.strip() for s in str(scope_text).split(" ") if s.strip()]
    _STATE["principal"] = payload.get("email") or payload.get("sub")
    _STATE["principal_verified"] = bool(payload.get("email"))
    return granted


def _past_expiry(creds: Any) -> bool:
    """Whether the held token has passed its `expiry` (naive UTC, as google-auth stores it).

    No `expiry` means it never expires, google-auth's own rule.
    """
    expiry = getattr(creds, "expiry", None)
    return expiry is not None and expiry <= datetime.now(UTC).replace(tzinfo=None)


def _access_token(creds: Any) -> str:
    """The token to introspect, refreshing the credential if it holds no live one.

    Done here because `AuthorizedSession` would otherwise refresh inside its first request, before
    layer 1 has passed. The handshake itself is google-auth's and is not seen by the guard or the
    lint. A failed handshake refuses.
    """
    token = getattr(creds, "token", None)
    if token and not _past_expiry(creds):
        return str(token)
    try:
        creds.refresh(Request())
    except (auth_exceptions.GoogleAuthError, requests.exceptions.RequestException) as exc:
        # `GoogleAuthError` covers google-auth's own errors; `RequestException` a credential that
        # raises the transport error itself.
        raise ScopeRefusal(
            "the credential handshake failed, so the granted scope set is unknown "
            f"({type(exc).__name__}: {exc}); refusing to run") from exc
    token = getattr(creds, "token", None)
    if not token:
        raise ScopeRefusal("the credential handshake returned no access token, so the granted "
                           "scope set is unknown; refusing to run")
    return str(token)


def _check_scopes(granted: list[str]) -> None:
    if not granted:
        raise ScopeRefusal("the token introspected to zero granted scopes; refusing to run")
    extra = sorted(set(granted) - SCOPE_ALLOWLIST)
    if extra:
        raise ScopeRefusal(
            "the granted scope set is wider than this toolkit admits; refusing to run. "
            f"Unexpected: {', '.join(extra)}. This pass would run read-only by construction, "
            "but the point of layer 1 is that a token able to write is never used to test that.")


def make_session(*, scopes: tuple[str, ...] = NARROW_SCOPES) -> Any:
    """Adopt application-default credentials, verify them, and return the guarded session.

    Order is the claim: mint the token, introspect it, check it, and only then build the session,
    so a refusal leaves no session to make a request with.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if scopes != NARROW_SCOPES:
        raise ScopeRefusal(f"this toolkit runs with exactly {len(NARROW_SCOPES)} scopes; "
                           "asking for a different set is not supported")
    if not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or os.path.exists(os.path.expanduser("~/.config/gcloud/application_default_credentials.json"))):
        raise ScopeRefusal(
            "no application-default credential found; set GOOGLE_APPLICATION_CREDENTIALS to the "
            "auditor service account's key. Whatever credential is found, layer 1 refuses it unless "
            "every scope it was granted is on the read-only list.")
    creds, _project = google.auth.default(scopes=list(NARROW_SCOPES))
    granted = _granted_scopes(_access_token(creds))
    _check_scopes(granted)
    inner = AuthorizedSession(creds)
    _STATE.update(state="ENFORCED", granted=sorted(granted), verified_via="tokeninfo",
                  reason="every request is one of the named reads; granted scopes were read back "
                         "off the token-introspection endpoint, not off the credential")
    _SESSION = GuardedSession(inner)
    return _SESSION


def session() -> Any:
    return _SESSION if _SESSION is not None else make_session()


def install(session: Any) -> None:
    """Test seam: put a fake session in place of `make_session`'s.

    Fakes subclass `GuardedSession` and override only `_dispatch`, so the real admission code runs.
    """
    global _SESSION
    _SESSION = session
    if _STATE["state"] == "REFUSED":
        _STATE.update(state="ENFORCED", granted=sorted(SCOPE_ALLOWLIST),
                      verified_via="installed-by-test",
                      reason="a test installed this session; no token was introspected")


def reset() -> None:
    """Drop the session and counters. Between tests, and after a refusal."""
    global _SESSION
    _SESSION = None
    _STATE.update(state="REFUSED", granted=[], verified_via="none", principal=None,
                  principal_verified=False)
    for key in _STATS:
        _STATS[key] = 0


def layer1_status() -> dict[str, Any]:
    return {**_STATE, "allowed": sorted(SCOPE_ALLOWLIST)}


def layer1_line() -> str:
    """The README.txt sentence about the credential."""
    state = _STATE["state"]
    if state != "ENFORCED":
        return f"layer1 = REFUSED ({_STATE['reason']})"
    who = _STATE["principal"] if _STATE["principal_verified"] else "principal not verified"
    return (f"layer1 = ENFORCED — {len(_STATE['granted'])} granted scopes read back off "
            f"tokeninfo, all inside the allowlist; {who}; every request was one of the "
            f"{methods.READ_COUNT} named reads")


# ------------------------------------------------------------------- layer 3

def _admit(read: methods.Read, url: str, params: dict[str, str], verb: str,
           query: dict[str, Any] | None, body: dict[str, Any] | None) -> None:
    """The last gate before the wire. Raises `MethodNotAllowed`; never warns."""
    reason = read.admits(params=params, query=query, body=body)
    if reason:
        raise methods.MethodNotAllowed(reason)
    if read.verb != str(verb).upper():
        raise methods.MethodNotAllowed(
            f"{read.method_id}: declared as {read.verb}; refusing to send it as {verb}")
    if not _is_tokeninfo(str(verb).upper(), url):
        built = read.url_of(params)
        if built != url:
            raise methods.MethodNotAllowed(
                f"{read.method_id}: the URL asked for ({url}) is not the one this method builds "
                f"({built}); refusing")
        if methods.match(str(verb).upper(), url) is None:
            raise methods.MethodNotAllowed(f"{url} matches no allowlisted path")


class GuardedSession:
    """Wraps an AuthorizedSession so the only way through is an admitted read.

    `_dispatch` is the one method that touches the network. Redirects are off: a redirect is a
    second, unadmitted request.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def read(self, method_id: str, *, params: dict[str, str],
             query: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request(method_id, params, query=query, body=body)

    def request(self, method_id: str, params: dict[str, str], *,
                query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Admit, count, then send. The cap lives here, not in `_dispatch`, so test fakes obey it."""
        if _STATS["requests"] >= MAX_REQUESTS:
            raise ReadFailed(method_id, None,
                             f"the {MAX_REQUESTS}-request cap for one process was reached")
        read = methods.lookup(method_id)
        url = read.url_of(params)
        _admit(read, url, params, read.verb, query, body)
        _STATS["requests"] += 1
        return self._dispatch(method_id, read.verb, url, query, body)

    def _dispatch(self, method_id: str, verb: str, url: str,
                  query: dict[str, Any] | None,
                  body: dict[str, Any] | None) -> dict[str, Any]:
        attempt = 0
        while True:
            reply = self._inner.request(verb, url, params=query, json=body,
                                        timeout=TIMEOUT_SECONDS, allow_redirects=False)
            status = getattr(reply, "status_code", 200)
            if status in RETRY_STATUS and attempt < len(BACKOFF_SECONDS):
                _STATS["retries"] += 1
                time.sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            if status >= 400:
                raise ReadFailed(method_id, status, _reason(reply))
            try:
                return reply.json()
            except ValueError as exc:
                raise ReadFailed(method_id, status,
                                 f"answered {type(exc).__name__} instead of JSON") from exc


def _reason(reply: Any) -> str:
    try:
        payload = reply.json()
        err = payload.get("error") or {}
        return f"{err.get('status', '')} {err.get('message', '')}".strip()[:200] or "no reason given"
    except Exception:
        return (getattr(reply, "reason", "") or "no reason given")[:200]


def paged(method_id: str, *, params: dict[str, str],
          query: dict[str, Any] | None = None,
          body: dict[str, Any] | None = None,
          max_pages: int = 200) -> Iterator[dict[str, Any]]:
    """Every page of one collection read. Raises at `max_pages` rather than return a truncated
    set that looks complete."""
    seen = 0
    token: str | None = None
    while True:
        this = dict(query or {})
        if token:
            this["pageToken"] = token
        _STATS["pages"] += 1
        payload = session().request(method_id, params, query=this, body=body)
        seen += 1
        yield payload
        token = payload.get("nextPageToken")
        if not token:
            return
        if seen >= max_pages:
            raise ReadFailed(method_id, None,
                             f"stopped after {max_pages} pages with a page token outstanding; "
                             "this universe is incomplete and the pass must not print it as "
                             "complete")


def stats() -> dict[str, int]:
    return dict(_STATS)
