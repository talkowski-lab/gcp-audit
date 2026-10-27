"""projects.py — which projects a run covers, and which APIs they have switched on.

* A typed project id is verified by `projects.get` and kept only if the cloud answers `ACTIVE`.
* `--folder` lists that folder (`projects.list` with `parent`). `--all` and a `--label` without a
  folder use `projects.search`, which returns every project the credential can see; its `query`
  ORs multiple terms, so at most one `labels.<key>:<value>` term is sent and both the label and
  `ACTIVE` are re-checked on every row.
* The guard checks query keys, not values, so every value's shape (folder id, project id, label) is
  validated here and a bad one is `guard.AuditError`.

A `ReadFailed` on a list or search stops the run: an empty list means "looked, found nothing".
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from gcp_audit import guard, model

PROJECTS_READ = "cloudresourcemanager.projects.list"
PROJECT_READ = "cloudresourcemanager.projects.get"
SEARCH_READ = "cloudresourcemanager.projects.search"
SERVICES_READ = "serviceusage.services.list"

#: One page size for both paged reads; `serviceusage.services.list` caps at 200.
PAGE_SIZE = 200

#: Folder ids are 4 to 30 digits. Checked here because `parent` is a query value, which the guard
#: does not pattern-check.
FOLDER_ID_RE = re.compile(r"^[0-9]{4,30}$")

#: Looser than the v3 document's pattern (admits a leading digit or underscore): refusing to match a
#: label a project really carries would hide a live project.
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")

ENABLED_FILTER = "state:ENABLED"


def _project_id(value: object) -> str:
    """A project id, or an `AuditError` with a message rather than a guard refusal mid-loop."""
    text = str(value)
    if not model.PROJECT_ID_RE.match(text):
        raise guard.AuditError(
            f"{value!r} is not a project id: 6 to 30 lowercase letters, digits and hyphens, "
            "starting with a letter and not ending with a hyphen")
    return text


def _project_name(value: object) -> str:
    """`projects/<id>`, the `name` placeholder of `projects.get`, validated before it is built."""
    return f"projects/{_project_id(value)}"


def _folder_parent(folder: object) -> str:
    text = str(folder)
    if not FOLDER_ID_RE.match(text):
        raise guard.AuditError(
            f"folder {folder!r} is not a folder id: Resource Manager folder ids are 4 to 30 "
            "digits. The guard admits the `parent` parameter but checks keys, not values, so "
            "this is where the value's shape is decided.")
    return f"folders/{text}"


def _label_pair(label: object) -> tuple[str, str]:
    """`key=value` or `AuditError`: a typo must not come back as "no projects"."""
    text = str(label)
    key, sep, value = text.partition("=")
    if not sep:
        raise guard.AuditError(f"label {label!r} is not KEY=VALUE")
    if not model.LABEL_RE.match(key):
        raise guard.AuditError(
            f"label key {key!r} does not match model.LABEL_RE: a lowercase letter, then up to "
            "62 lowercase letters, digits, underscores and hyphens")
    if not LABEL_VALUE_RE.match(value):
        raise guard.AuditError(
            f"label value {value!r} is not a label value: up to 63 lowercase letters, digits, "
            "underscores and hyphens (empty is allowed)")
    return key, value


def _carries_label(project: dict, key: str, value: str) -> bool:
    labels = project.get("labels")
    if not isinstance(labels, dict):
        return False
    return key in labels and str(labels[key]) == value


def _narrows(row: dict, key: str | None, value: str | None) -> bool:
    """Whether a project row passes the label selector. No selector, no narrowing."""
    if key is None or value is None:
        return True
    return _carries_label(row, key, value)


def _pages(parent: str) -> Iterator[dict]:
    """Every page of `projects.list` under one parent. Raises on a refusal: an incomplete folder is
    not a smaller scope."""
    for page in guard.paged(PROJECTS_READ, params={},
                            query={"parent": parent, "pageSize": PAGE_SIZE}):
        yield from page.get("projects") or []


def _search(key: str | None, value: str | None) -> Iterator[dict]:
    """Every page of `projects.search`, optionally narrowed server-side by one label term. The
    search is case-insensitive and eventually consistent, so `_narrows` and `_live` decide."""
    query: dict[str, object] = {"pageSize": PAGE_SIZE}
    if key is not None:
        query["query"] = f"labels.{key}:{value or '*'}"
    for page in guard.paged(SEARCH_READ, params={}, query=query):
        yield from page.get("projects") or []


def _named_refusal(project_id: str) -> str:
    """Why `projects.get` did not confirm *this* project id as ACTIVE, or "" if it did. A refusal
    or a reply naming another id confirms nothing, so the id stays out of scope."""
    try:
        reply = guard.session().request(PROJECT_READ, params={"name": _project_name(project_id)})
    except guard.ReadFailed as exc:
        return str(exc)
    if reply.get("projectId") != project_id:
        return f"{PROJECT_READ} answered with a different project id"
    if reply.get("state") != "ACTIVE":
        return f"the project's state is {reply.get('state') or 'not reported'}, not ACTIVE"
    return ""


def _live(page_projects: Iterator[dict]) -> Iterator[dict]:
    """The rows whose own `state` says ACTIVE; a row with no state is unobserved, not active."""
    for row in page_projects:
        if row.get("projectId") and row.get("state") == "ACTIVE":
            yield row


def resolve_projects(*, projects: Sequence[str] = (), folder: str | None = None,
                     label: str | None = None,
                     everything: bool = False,
                     refusals: dict[str, str] | None = None) -> list[str]:
    """The scope of a run, as sorted deduplicated ACTIVE project ids.

    A typed id the cloud does not confirm is left out, not raised: "you named nothing I can see" is
    an empty list and `cli.py`'s error to print. A folder contributes its ACTIVE projects, narrowed
    by the label if given; `--all`, or a label without a folder, searches everything the credential
    can see. No selector at all is an `AuditError`, since nothing was looked at. If `refusals` is
    given, it receives each unconfirmed typed id and the reason.
    """
    named = [_project_id(p) for p in projects]
    parent = _folder_parent(folder) if folder is not None else None
    key: str | None = None
    value: str | None = None
    if label is not None:
        key, value = _label_pair(label)
    if not named and folder is None and label is None and not everything:
        raise guard.AuditError(
            "no scope was given: name a --project, a --folder, a --label or --all. An empty "
            "result here would mean 'looked and found nothing', and nothing has been looked at.")

    scope: set[str] = set()

    for project_id in dict.fromkeys(named):
        # One read per distinct id, in the order typed.
        reason = _named_refusal(project_id)
        if not reason:
            scope.add(project_id)
        elif refusals is not None:
            refusals[project_id] = reason

    if parent is not None:
        scope.update(str(row["projectId"]) for row in _live(_pages(parent))
                     if _narrows(row, key, value))
    elif everything or key is not None:
        scope.update(str(row["projectId"]) for row in _live(_search(key, value))
                     if _narrows(row, key, value))

    return sorted(scope)


def enabled_services(project: str) -> tuple[list[str], str | None]:
    """(enabled API domains, gap reason) for one project, from `serviceusage.services.list`.

    A row whose own `state` contradicts the `ENABLED` filter is dropped; a row with no `state`
    stays, since this list says what the table does not cover. The reason is `None` only when the list can
    be trusted: "nothing else is on" and "could not read what is on" are different answers.
    """
    parent = f"projects/{_project_id(project)}"
    names: list[str] = []
    reason: str | None = None
    try:
        pages = guard.paged(SERVICES_READ, params={"parent": parent},
                            query={"filter": ENABLED_FILTER, "pageSize": PAGE_SIZE})
        for page in pages:
            for service in page.get("services") or []:
                state = service.get("state")
                if state is not None and state != "ENABLED":
                    continue
                api = _api_domain(service)
                if api:
                    names.append(api)
                elif reason is None:
                    reason = ("serviceusage.services.list answered a service with no name, so "
                              "the enabled list may be short")
    except guard.ReadFailed as exc:
        status = "" if exc.status is None else f"HTTP {exc.status} "
        reason = f"{exc.method_id}: {status}{exc.reason}".strip()
    return sorted(set(names)), reason


def _api_domain(service: dict) -> str:
    """The API domain of one services.list row: `config.name`, else the last segment of `name`."""
    config = service.get("config")
    if isinstance(config, dict) and config.get("name"):
        return str(config["name"])
    name = service.get("name")
    return str(name).rpartition("/")[2] if name else ""
