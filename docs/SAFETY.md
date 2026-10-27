# Safety

**`gca` never changes a cloud resource.** No flag, option or request from a user makes it do so.
A cost auditor that can delete a disk is one typo away from an outage.

Three independent layers enforce this. Each section below says exactly what that layer
checks.

## 1. Scopes, checked against the issued token

`guard.make_session` asks for five read-only scopes (`guard.NARROW_SCOPES`):

* `cloud-platform.read-only`
* `devstorage.read_only`
* `compute.readonly`
* `monitoring.read`
* `cloud-billing.readonly`

It then asks Google's `tokeninfo` endpoint which scopes the token was actually granted, and
refuses to run unless every one of them is in that list. An empty or unreadable answer is also a
refusal. It does not trust `creds.scopes`, which records what the client asked for, not what the
server granted.

The broad `cloud-platform` scope can write, so it is never accepted, and there is no override. A
personal `gcloud auth application-default login` token carries `cloud-platform`, so `gca` refuses
it. A run with no application-default credential also refuses.

This is why some services are not read at all (GKE, Pub/Sub, Dataproc, Cloud Functions, Cloud Run,
Artifact Registry, Cloud SQL and Spanner): their APIs have no read-only scope. BigQuery has no
read-only scope of its own either, but its APIs accept `cloud-platform.read-only`.
[`API-EVIDENCE.md`](API-EVIDENCE.md) lists which API declares each scope.

## 2. An allowlist of twenty requests

`src/gcp_audit/methods.py` lists every request `gca` can make. Each entry gives the method id,
host, HTTP verb, path template, permitted query and body keys, and a pattern for each path
placeholder. Any other request is refused before a connection is opened.

* Collectors never build URLs. They name a method and its parameters, and the guard builds the URL
  from the template. Each parameter must match its pattern, which forbids `/`, `?`, `#` and `..`.
  The finished URL is then matched against the allowlist a second time.
* **Requests are allowed by method id, not by HTTP verb.** The verb says nothing reliable about
  whether a request writes: `cloudbilling.billingAccounts.move` is a GET that changes things, and
  `bigquery.jobs.query` is a POST that only reads.
* `guard.GuardedSession._dispatch` is the only code that sends network traffic. It does not follow
  redirects, retries at most twice (only on 429 and 5xx responses), and stops after
  `guard.MAX_REQUESTS` requests per run, so a paging bug cannot run away.
* The one POST, `bigquery.jobs.query`, is allowed only with `--read-bq-storage`, and only with a
  fixed set of body keys. **This layer does not inspect the SQL.** The only query `gca` sends
  is the constant `collect_bigquery.TABLE_STORAGE_SQL`, a `SELECT` into which only a validated
  project id and location are inserted. Layer 3 and code review keep it that way.

## 3. A lint check over the source

`tools/lint_readonly.py` parses every Python file in `src/`, `tests/` and `tools/` and fails on:

* imports of modules that can reach the network or run programs: `subprocess`, `socket`,
  `urllib.request`, `http.client`, `urllib3`, `asyncio`, `multiprocessing`, `ctypes`, `pty`,
  `fcntl`, `importlib`, the local `admin` setup scripts, and `requests` anywhere except
  `guard.py`;
* `os.system`, `os.popen`, the `os.exec*` and `os.spawn*` functions, `eval`, `exec`, `compile`,
  `__import__` and `breakpoint`;
* method calls named `post`, `put`, `patch`, `delete`, `head`, `options`, `send`, `request_raw`,
  `urlopen`, `open_connection` or `connection`, and `allow_redirects=True`;
* `getattr` with a name computed at run time, which could hide any of the calls above.

A `# lint-allow` comment exempts only its own line and must give a reason; the number of them in
each file is pinned. There are two standing exceptions: `guard.py` imports `requests` for the
`tokeninfo` check, which cannot go through the session it is verifying, and `tests/conftest.py`
imports `socket` in order to block it. `tests/test_readonly_lint.py` plants each kind of violation
in a temporary file and fails if the lint misses it.

## `gca setup`

`gca setup` sends no request. It validates the account names you give it against strict patterns,
shell-quotes them, and writes a `gcloud` script to a local file. The script changes IAM (it creates
the service account, a key, and role bindings) and runs under your own login, outside all three
layers. That is why it is a file you read and run yourself, with grants printed as a dry run until
you add `--apply`.

## What is not checked

* **IAM roles.** Nothing reads IAM policies, and no output claims that roles were checked.
* **`testIamPermissions`.** `gca` never calls it; it is not on the allowlist. (The script that
  `gca setup` writes does call it, under your own login, to find projects where you can grant roles.)
* **IAM conditions, groups and domains.** None are evaluated.
* **Whether the account could write.** The layers stop `gca` from sending a write. They do not
  prove that the account's roles are read-only, so grant only read-only roles
  ([`SETUP.md`](SETUP.md)).
* **Obtaining the token.** `google-auth` gets the token over its own HTTP connection, outside the
  guard. That exchange and the `tokeninfo` check are the only requests sent before layer 1
  has passed.

## Tests

* **No test opens a network connection.** `tests/conftest.py` blocks `connect`, `connect_ex`,
  `sendto`, `create_connection`, `getaddrinfo` and `google.auth.default` for the whole suite.
* Test fakes subclass `guard.GuardedSession` and replace only `_dispatch`, so the tests exercise
  the real admission checks.
* Nothing in the repo authenticates to Google. Questions that need real credentials go in
  [`DESIGN.md`](DESIGN.md) §Open questions.
