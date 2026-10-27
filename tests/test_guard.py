"""Layer 1 (scope verification) and the guarded session that refuses everything else.

No test here reaches the network. `conftest.py` blocks connections, and `tokeninfo` answers from a
staged reply. A live attach uses a fake credential that mints a token when refreshed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fake_session import install
from google.auth import exceptions as auth_exceptions
from google.auth.transport.requests import Request

from gcp_audit import cli, guard
from gcp_audit import methods as M


@pytest.fixture(autouse=True)
def clean_guard():
    guard.reset()
    yield
    guard.reset()


class _Reply:
    def __init__(self, payload=None, status=200, text=None):
        self._payload, self.status_code, self._text = payload, status, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _BadInner:
    """Records what the guarded session passes down: one verb, one URL, no redirects."""

    def __init__(self):
        self.seen = []

    def request(self, verb, url, **kwargs):
        self.seen.append((verb, url, kwargs))
        return _Reply({"ok": True})


# ---------------------------------------------------------------------- layer 1

def test_a_wider_grant_refuses_and_names_the_scope(monkeypatch):
    with pytest.raises(guard.ScopeRefusal) as err:
        guard._check_scopes(list(guard.NARROW_SCOPES)
                            + ["https://www.googleapis.com/auth/cloud-platform"])
    assert "cloud-platform" in str(err.value)


def test_the_asked_for_set_is_exactly_the_five_declared_scopes():
    """The scopes are pinned by name, so a sixth cannot be added unnoticed.

    `docs/API-EVIDENCE.md` §The five scopes a run may hold lists which API declares each one.
    `bigquery.readonly` is not a scope any Google API declares, so it must stay out.
    """
    assert guard.NARROW_SCOPES == (
        "https://www.googleapis.com/auth/cloud-platform.read-only",
        "https://www.googleapis.com/auth/devstorage.read_only",
        "https://www.googleapis.com/auth/compute.readonly",
        "https://www.googleapis.com/auth/monitoring.read",
        "https://www.googleapis.com/auth/cloud-billing.readonly",
    )
    assert len(guard.NARROW_SCOPES) == 5
    assert guard.SCOPE_ALLOWLIST == frozenset(guard.NARROW_SCOPES)
    assert "https://www.googleapis.com/auth/bigquery.readonly" not in guard.SCOPE_ALLOWLIST


def test_every_read_in_the_table_is_covered_by_one_of_the_five():
    """Each of the twenty reads needs only scopes from the five.

    `methods.Read.scopes` uses the short names. An empty tuple would pass the subset check
    vacuously, so each tuple must also be non-empty."""
    short = {s.rsplit("/", 1)[-1] for s in guard.NARROW_SCOPES}
    assert M.READS, "the allowlist is empty, which would make this test prove nothing"
    for read in M.READS.values():
        assert read.scopes, f"{read.method_id} declares no scope at all"
        assert set(read.scopes) <= short, f"{read.method_id} needs a scope outside the five"


def test_an_empty_grant_refuses_rather_than_defaulting_to_allow():
    """An unknown grant must refuse, never fall through to allow."""
    with pytest.raises(guard.ScopeRefusal):
        guard._check_scopes([])


def test_a_narrow_grant_is_accepted_and_recorded():
    guard._check_scopes(list(guard.NARROW_SCOPES))
    guard._STATE.update(state="ENFORCED", granted=sorted(guard.NARROW_SCOPES))
    assert guard.layer1_status()["state"] == "ENFORCED"
    assert "ENFORCED" in guard.layer1_line()


def test_no_override_admits_cloud_platform():
    """No flag, environment variable or argument widens the scope allowlist."""
    assert "cloud-platform" not in {s.rsplit("/", 1)[-1] for s in guard.SCOPE_ALLOWLIST
                                    if not s.endswith("read-only")}
    assert "GCA_ALLOW_FULL_SCOPE" not in open("src/gcp_audit/guard.py", encoding="utf-8").read()


def test_tokeninfo_answer_is_what_counts_not_the_credential_claim(monkeypatch):
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        return _Reply({"scope": " ".join(guard.NARROW_SCOPES) + " extra@noop",
                       "email": "auditor@lab.iam.gserviceaccount.com"})

    monkeypatch.setattr(guard.requests, "get", fake_get)
    granted = guard._granted_scopes("ya29.token")
    assert calls["url"] == f"https://{guard.TOKENINFO_HOST}{guard.TOKENINFO_PATH}"
    assert granted == list(guard.NARROW_SCOPES) + ["extra@noop"]
    assert guard._STATE["principal"] == "auditor@lab.iam.gserviceaccount.com"
    with pytest.raises(guard.ScopeRefusal):
        guard._check_scopes(granted)


def test_an_introspection_failure_refuses_instead_of_assuming(monkeypatch):
    for payload, status in [({"error": "invalid_token"}, 400), (None, 200)]:
        monkeypatch.setattr(guard.requests, "get",
                            lambda url, params=None, timeout=None, p=payload, s=status:
                            _Reply(p, s))
        with pytest.raises(guard.ScopeRefusal):
            guard._granted_scopes("ya29.token")
    with pytest.raises(guard.ScopeRefusal):
        guard._granted_scopes("")


def test_a_run_without_a_configured_credential_refuses(monkeypatch, tmp_path):
    """No key and no gcloud ADC file: refuse before anything is resolved."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(guard.ScopeRefusal) as err:
        guard.make_session()
    assert "application-default" in str(err.value)


def test_asking_for_a_different_scope_set_is_not_supported():
    with pytest.raises(guard.ScopeRefusal):
        guard.make_session(scopes=("https://www.googleapis.com/auth/cloud-platform",))


# ------------------------------------------------------------ the live attach

def _utc(offset: timedelta) -> datetime:
    """A naive UTC datetime, which is the shape google-auth stores `expiry` in."""
    return datetime.now(UTC).replace(tzinfo=None) + offset


class _FakeCreds:
    """A staged google-auth credential with only the fields the guard reads.

    It has no `scopes` attribute unless a test sets one, so a guard that trusted the credential's
    claim would fail. `refresh` records the request object it gets. `mints` is the token a refresh
    leaves behind, or None for a refresh that yields no token.
    """

    def __init__(self, *, token=None, expiry=None, scopes=None, raises=None,
                 mints="ya29.minted-by-the-handshake"):
        self.token, self.expiry, self.raises, self.mints = token, expiry, raises, mints
        self.refresh_calls: list = []
        if scopes is not None:
            self.scopes = list(scopes)

    def refresh(self, request):
        self.refresh_calls.append(request)
        if self.raises is not None:
            raise self.raises
        self.token = self.mints


class _Inner:
    """Stands in for `AuthorizedSession` and records anything it is asked to send.

    After a refusal, nothing may have been sent through it.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def request(self, verb, url, **kwargs):
        self.sent.append((verb, url))
        raise AssertionError("a resource request went out through the session")


class _Live:
    """What one staged attach saw: the scopes requested and the tokens introspected."""

    def __init__(self, creds: _FakeCreds) -> None:
        self.creds = creds
        self.inner = _Inner()
        self.requested: list[str] = []
        self.introspections: list[dict] = []
        self.session_built = False

    def tokens_introspected(self) -> list:
        return [i["params"]["access_token"] for i in self.introspections]


def _stage_live(monkeypatch, creds: _FakeCreds, *, granted: str = " ".join(guard.NARROW_SCOPES),
                email="auditor@lab.iam.gserviceaccount.com") -> _Live:
    """Stage one live `make_session()` offline.

    `google.auth.default` is replaced on the module object, so its metadata-server probe and
    user-login fallback cannot run. `tokeninfo` answers through a staged `guard.requests.get`.
    """
    live = _Live(creds)

    def fake_default(scopes=None, **_kwargs):
        live.requested = list(scopes or [])
        return creds, "lab-seq-prd"

    def fake_get(url, params=None, timeout=None):
        live.introspections.append({"url": url, "params": params, "timeout": timeout})
        return _Reply({"scope": granted, "email": email})

    def fake_authorized_session(given):
        assert given is creds, "the session must wrap the credential that was just verified"
        live.session_built = True
        return live.inner

    monkeypatch.setattr(guard.google.auth, "default", fake_default)
    monkeypatch.setattr(guard.requests, "get", fake_get)
    monkeypatch.setattr(guard, "AuthorizedSession", fake_authorized_session)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/staged/auditor-key.json")
    return live


def _assert_refused_before_anything_asked(live: _Live) -> None:
    """A refusal leaves no session, sends nothing and leaves the request counter at zero."""
    assert not live.session_built
    assert live.inner.sent == []
    assert guard._SESSION is None
    assert guard.stats()["requests"] == 0
    assert guard.layer1_status()["state"] == "REFUSED"


def test_a_fresh_credential_is_refreshed_before_anything_is_introspected(monkeypatch):
    """A new credential has no token until refreshed, and `AuthorizedSession` refreshes only on
    its first request. So `make_session` must refresh once itself before calling `tokeninfo`."""
    creds = _FakeCreds()
    live = _stage_live(monkeypatch, creds)

    session = guard.make_session()

    assert live.requested == list(guard.NARROW_SCOPES)
    assert not hasattr(creds, "scopes"), "the narrow pass completes without the credential's claim"
    assert len(creds.refresh_calls) == 1, "one handshake for one attach, not one per request"
    assert isinstance(creds.refresh_calls[0], Request)
    assert live.tokens_introspected() == [creds.mints]
    assert live.session_built and live.inner.sent == []
    assert isinstance(session, guard.GuardedSession)
    assert guard.stats()["requests"] == 0, "verifying a credential is not a resource read"
    assert guard.layer1_status()["state"] == "ENFORCED"


def test_an_unexpired_token_is_used_as_it_stands_and_not_refreshed(monkeypatch):
    """A token that has not expired is introspected as is, without a refresh."""
    creds = _FakeCreds(token="ya29.still-good", expiry=_utc(timedelta(hours=1)))
    live = _stage_live(monkeypatch, creds)

    session = guard.make_session()
    again = guard.make_session()

    assert creds.refresh_calls == []
    assert live.tokens_introspected() == ["ya29.still-good"]
    assert again is session


def test_an_expired_token_is_refreshed_rather_than_introspected(monkeypatch):
    """The staged tokeninfo answer is the same for both tokens, so the test checks which token
    the guard put in the query."""
    creds = _FakeCreds(token="ya29.expired", expiry=_utc(-timedelta(hours=1)))
    live = _stage_live(monkeypatch, creds)

    guard.make_session()

    assert len(creds.refresh_calls) == 1
    assert live.tokens_introspected() == [creds.mints]


def test_the_grant_is_what_tokeninfo_says_even_when_the_credential_says_five(monkeypatch):
    """`creds.scopes` is the credential's claim; tokeninfo is the server's grant, and only the
    grant counts. A credential that claims the five narrow scopes but holds a `cloud-platform`
    token must be refused."""
    creds = _FakeCreds(scopes=list(guard.NARROW_SCOPES))
    live = _stage_live(monkeypatch, creds,
                       granted="https://www.googleapis.com/auth/cloud-platform")

    with pytest.raises(guard.ScopeRefusal) as err:
        guard.make_session()

    assert "cloud-platform" in str(err.value)
    assert "able to write" in str(err.value), "the message must say why a writable token is out"
    assert len(creds.refresh_calls) == 1, "the handshake is what produced the token introspected"
    _assert_refused_before_anything_asked(live)


def test_an_empty_grant_refuses_after_the_handshake_rather_than_defaulting_to_allow(monkeypatch):
    creds = _FakeCreds()
    live = _stage_live(monkeypatch, creds, granted="")

    with pytest.raises(guard.ScopeRefusal) as err:
        guard.make_session()

    assert "zero granted scopes" in str(err.value)
    assert len(creds.refresh_calls) == 1
    _assert_refused_before_anything_asked(live)


@pytest.mark.parametrize("failure,cause", [
    (auth_exceptions.RefreshError("400 Bad Request: invalid_grant"), "invalid_grant"),
    (auth_exceptions.TransportError("POST https://oauth2.googleapis.com/token: name resolution"),
     "name resolution"),
    # google-auth normally wraps requests failures in `TransportError`; this covers a credential
    # that raises one directly. Reached through `guard.requests` because the lint keeps `requests`
    # out of `tests/`.
    (guard.requests.exceptions.ConnectionError("connection refused"), "connection refused"),
])
def test_a_handshake_failure_refuses_and_names_what_failed(monkeypatch, failure, cause):
    """A failed refresh must become a refusal that names the cause, not a traceback or a run.
    With no token the grant is unknown, and an unknown grant refuses."""
    creds = _FakeCreds(raises=failure)
    live = _stage_live(monkeypatch, creds)

    with pytest.raises(guard.ScopeRefusal) as err:
        guard.make_session()

    assert "handshake failed" in str(err.value)
    assert type(failure).__name__ in str(err.value), "name the cause, not just the refusal"
    assert cause in str(err.value)
    assert live.introspections == [], "there was no token to ask about"
    _assert_refused_before_anything_asked(live)


def test_a_handshake_that_mints_no_token_refuses_rather_than_assuming(monkeypatch):
    """A refresh that succeeds but leaves no token is also an unknown grant."""
    creds = _FakeCreds(mints=None)
    live = _stage_live(monkeypatch, creds)

    with pytest.raises(guard.ScopeRefusal) as err:
        guard.make_session()

    assert "no access token" in str(err.value)
    assert live.introspections == []
    _assert_refused_before_anything_asked(live)


def test_a_live_refusal_exits_two_and_writes_nothing(monkeypatch, tmp_path, capsys):
    """End to end: a wide grant found at a live attach exits with `EXIT_REFUSED` and writes no
    output directory."""
    _stage_live(monkeypatch, _FakeCreds(),
                granted="https://www.googleapis.com/auth/cloud-platform")
    out = tmp_path / "run"

    assert cli.main(["table", "--project", "lab-seq-prd", "-o", str(out)]) == 2

    assert "refusing to run" in capsys.readouterr().err.lower()
    assert not out.exists()


# ------------------------------------------------- layer 2: the guarded session

def test_an_admitted_read_goes_through_with_no_redirects():
    inner = _BadInner()
    session = guard.GuardedSession(inner)
    payload = session.request("compute.instances.aggregatedList", {"project": "lab-seq-prd"},
                              query={"maxResults": 500})
    assert payload == {"ok": True}
    verb, url, kwargs = inner.seen[0]
    assert verb == "GET"
    assert url == "https://compute.googleapis.com/compute/v1/projects/lab-seq-prd/aggregated/instances"
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == guard.TIMEOUT_SECONDS


def test_a_write_verb_cannot_be_sent_even_by_calling_the_internals():
    """`request` takes the verb from the allowlist entry, and `_admit` checks it again."""
    inner = _BadInner()
    with pytest.raises(M.MethodNotAllowed):
        guard._admit(M.lookup("compute.instances.aggregatedList"),
                     "https://compute.googleapis.com/compute/v1/projects/lab-seq-prd"
                     "/aggregated/instances",
                     {"project": "lab-seq-prd"}, "DELETE", None, None)
    assert inner.seen == []


def test_a_hand_built_url_is_refused_even_for_a_real_method():
    read = M.lookup("storage.buckets.list")
    with pytest.raises(M.MethodNotAllowed):
        guard._admit(read, "https://storage.googleapis.com/storage/v1/b?project=other",
                     {}, "GET", {"project": "other"}, None)
    # the same method, built the way the template says, is fine
    guard._admit(read, read.url_of({}), {}, "GET", {"project": "lab-seq-prd"}, None)


def test_a_404_is_a_read_failure_that_names_its_cause():
    install(routes={}, failures={"storage.buckets.list": (403, "PERMISSION_DENIED no access")})
    with pytest.raises(guard.ReadFailed) as err:
        guard.session().request("storage.buckets.list", params={}, query={"project": "p"})
    assert err.value.status == 403
    assert "PERMISSION_DENIED" in err.value.reason


def test_a_guard_refusal_is_not_a_read_failure():
    """Callers catch `ReadFailed` and record a gap. A guard refusal must not be caught that way,
    because the run should stop on it, not continue."""
    assert not issubclass(M.MethodNotAllowed, guard.ReadFailed)
    assert not issubclass(guard.ReadFailed, M.MethodNotAllowed)


def test_paged_follows_the_token_and_stops_loudly_at_its_cap():
    fake = install(routes={"compute.images.list": [
        {"images": [{"name": "a"}], "nextPageToken": "TOK"},
        {"images": [{"name": "b"}], "nextPageToken": "TOK2"},
        {"images": [{"name": "c"}], "nextPageToken": "TOK3"},
    ]})
    # A caller that catches the cap keeps the rows it has and records why the set is partial.
    pages, error = [], None
    try:
        for page in guard.paged("compute.images.list", params={"project": "lab-seq-prd"},
                                max_pages=2):
            pages.append(page)
    except guard.ReadFailed as exc:
        error = exc
    assert [p["images"][0]["name"] for p in pages] == ["a", "b"]
    assert error is not None and "incomplete" in str(error)
    assert fake.queries("compute.images.list")[1]["pageToken"] == "TOK"


def test_a_run_stops_at_the_request_cap_rather_than_at_an_invoice(monkeypatch):
    fake = install(routes={"compute.images.list": {"images": []}})
    monkeypatch.setattr(guard, "MAX_REQUESTS", 3)
    for _ in range(3):
        guard.session().request("compute.images.list", params={"project": "lab-seq-prd"})
    assert guard.stats()["requests"] == 3
    with pytest.raises(guard.ReadFailed) as err:
        guard.session().request("compute.images.list", params={"project": "lab-seq-prd"})
    assert "cap" in str(err.value)
    assert fake.count("compute.images.list") == 3


def test_the_installed_fake_still_enforces_the_allowlist():
    """The test fake overrides only `_dispatch`, so it still refuses methods off the allowlist."""
    fake = install(routes={"compute.images.list": {"images": []}})
    assert isinstance(fake, guard.GuardedSession)
    with pytest.raises(M.MethodNotAllowed):
        guard.session().request("compute.images.delete", params={"project": "lab-seq-prd"})
    with pytest.raises(M.MethodNotAllowed):
        guard.session().request("compute.images.list", params={"project": "Lab PRD"})


def test_layer1_line_names_what_was_checked_and_not_what_was_not():
    line = guard.layer1_line()
    assert "REFUSED" in line                       # nothing has been verified in this process
    install(routes={})
    line = guard.layer1_line()
    assert "installed-by-test" in line or "ENFORCED" in line
    assert "testIamPermissions" not in line        # nothing calls it; nothing may print it
    assert "role" not in line.lower()              # the role set is never read
