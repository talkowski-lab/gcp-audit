"""The scope of a run: which projects it covers, and which APIs they have enabled.

Payloads follow cloudresourcemanager v3 `Project` (`projectId`, `name`, `state`, `labels`,
`parent`, `displayName`), the same message from `projects.list` and `projects.get`, and
serviceusage v1 `GoogleApiServiceusageV1Service` (`name`, `state`, `parent`, `config`). See
`docs/API-EVIDENCE.md` §Resource Manager. Requests go through `tests/fake_session.py`, so a query
key the allowlist does not admit fails here as it would in a real run.
"""
from __future__ import annotations

import pytest
from fake_session import install

from gcp_audit import guard, methods, projects

PROJECTS_READ = "cloudresourcemanager.projects.list"
PROJECT_READ = "cloudresourcemanager.projects.get"
SERVICES_READ = "serviceusage.services.list"

#: The page size both paged reads ask for.
PAGE_SIZE = projects.PAGE_SIZE

A, B, C = "lab-alpha-seq", "lab-beta-gen", "lab-charlie-img"
FOLDER = "87654321"


def project(project_id: str, state: str = "ACTIVE", **extra) -> dict:
    """A v3 `Project`: the project number is in `name` and the id is in `projectId`."""
    row = {"projectId": project_id, "name": f"projects/{100000000 + len(project_id)}",
           "state": state, "displayName": project_id.replace("-", " ").title()}
    row.update(extra)
    return row


def page_rows(*rows: dict, **extra) -> dict:
    """One page of `projects.list`: these rows plus any extra page fields."""
    return {"projects": list(rows), **extra}


def listing(*pages: dict) -> dict:
    """The route for a `projects.list` that answers with exactly these pages."""
    return {PROJECTS_READ: list(pages)}


def get_project(*replies: dict) -> dict:
    """The route for `projects.get`: one `Project` per call, in call order across the run.

    `projects.get` is not paged, so each list entry answers one named project.
    """
    return {PROJECT_READ: list(replies)}


def service_list(*pages: dict) -> dict:
    return {SERVICES_READ: list(pages)}


def three_projects() -> dict:
    """Two live projects with two labels between them, and one marked for deletion."""
    return {PROJECTS_READ: [{"projects": [
        project(A, labels={"team": "genomics", "cost": "shared"}),
        project(B, labels={"team": "imaging"}),
        project(C, state="DELETE_REQUESTED", labels={"team": "genomics"}),
    ]}]}


def service(name: str, state: str = "ENABLED", **extra) -> dict:
    row = {"name": f"projects/415104041262/services/{name}", "state": state,
           "parent": "projects/415104041262"}
    row.update(extra)
    return row


# ------------------------------------------------------------- a project someone named

def test_a_named_project_is_verified_by_one_get_and_by_no_list():
    """v3 has no unscoped project list; `projects.list` without a parent returns HTTP 400
    (`docs/API-EVIDENCE.md` §Resource Manager). So a named project is checked with one `get`."""
    fake = install({PROJECT_READ: project(A)})
    assert projects.resolve_projects(projects=[A]) == [A]
    assert fake.count(PROJECT_READ) == 1
    assert fake.count(PROJECTS_READ) == 0
    assert fake.urls(PROJECT_READ) == [
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{A}"]


def test_each_named_project_is_asked_about_once_even_when_it_was_typed_twice():
    fake = install(get_project(project(B), project(A)))
    assert projects.resolve_projects(projects=[B, A, B]) == [A, B]
    assert fake.count(PROJECT_READ) == 2


def test_a_named_project_the_cloud_answered_nothing_about_is_absent():
    """A 404 and a 403 both leave the project out of the scope. Neither may yield an ACTIVE id.
    `test_cli_table.py` covers the exit code."""
    for answer in [(404, "NOT_FOUND Project not found"),
                   (403, "PERMISSION_DENIED Required permission resourcemanager.projects.get")]:
        fake = install({}, failures={PROJECT_READ: answer})
        assert projects.resolve_projects(projects=["lab-unlisted-project"]) == []
        assert fake.count(PROJECT_READ) == 1, "the id was never asked about, so nothing was learned"


def test_a_refused_get_stops_short_of_a_run_that_names_several_projects():
    """Unlike a refused folder read, which stops the run, a refused `get` drops only that one id.
    The other ids are still confirmed by the cloud."""
    fake = install(get_project(project(A)),
                   failures={(PROJECT_READ, B): (403, "PERMISSION_DENIED no grant here")})
    assert projects.resolve_projects(projects=[A, B]) == [A]
    assert fake.count(PROJECT_READ) == 2, "both ids were asked about; one answer refused"


def test_the_reason_a_typed_project_was_not_confirmed_is_kept():
    """A disabled API in the caller's own project looked like "zero projects" until the reason
    was passed back (seen live, 2026-10-28)."""
    install(get_project(project(A)),
            failures={(PROJECT_READ, B): (403, "PERMISSION_DENIED API has not been used")})
    refusals: dict[str, str] = {}
    assert projects.resolve_projects(projects=[A, B], refusals=refusals) == [A]
    assert list(refusals) == [B]
    assert "API has not been used" in refusals[B]


def test_the_typed_list_is_never_the_answer():
    """An id is returned only when the `get` reply confirms it. A reply naming a different
    project does not confirm the typed id."""
    fake = install(get_project(project("lab-someone-elses-proj")))
    assert projects.resolve_projects(projects=[A]) == []
    assert fake.count(PROJECT_READ) == 1


def test_a_named_project_the_cloud_marked_for_deletion_is_not_in_the_scope():
    """`state` decides, as it does for `projects.list`. A `DELETE_REQUESTED` project is left
    out."""
    install(get_project(project(A, state="DELETE_REQUESTED")))
    assert projects.resolve_projects(projects=[A]) == []


def test_an_answer_that_named_no_state_is_not_an_ACTIVE_project():
    """A reply with no `state` is not treated as ACTIVE."""
    install(get_project({"projectId": A, "name": "projects/415104041262"}))
    assert projects.resolve_projects(projects=[A]) == []


# ---------------------------------------------------------------- what a folder selects

def test_a_folder_becomes_a_parent_parameter_and_the_read_stays_small():
    """v3 `projects.list` has no `filter` but accepts `parent`. The query must hold exactly
    `parent` and the page size."""
    fake = install(listing(page_rows(project(A), project(B, state="DELETE_REQUESTED"))))
    assert projects.resolve_projects(folder=FOLDER) == [A]
    assert fake.urls(PROJECTS_READ) == ["https://cloudresourcemanager.googleapis.com/v3/projects"]
    assert fake.queries(PROJECTS_READ) == [{"parent": f"folders/{FOLDER}", "pageSize": PAGE_SIZE}]


def test_a_label_narrows_the_folder_it_was_given_a_folder_for():
    fake = install(listing(page_rows(project(A, labels={"team": "genomics"}),
                                      project(B, labels={"team": "imaging"}))))
    assert projects.resolve_projects(folder=FOLDER, label="team=genomics") == [A]
    assert fake.queries(PROJECTS_READ)[0]["parent"] == f"folders/{FOLDER}"


def test_a_folder_and_a_label_stay_on_one_read():
    """`parent` is the only server-side selector. The folder gives the candidates and the label
    is applied to them locally, in one read."""
    fake = install(listing(page_rows(
        project(A, labels={"team": "genomics"}, parent=f"folders/{FOLDER}"),
        project(B, labels={"team": "imaging"}, parent=f"folders/{FOLDER}"))))
    assert projects.resolve_projects(folder=FOLDER, label="team=genomics") == [A]
    assert fake.count(PROJECTS_READ) == 1


def test_a_label_that_matches_nothing_in_the_folder_is_an_answer_rather_than_a_refusal():
    """An empty result means every project in the folder was checked and none has the label.
    A malformed label, tested below, refuses instead."""
    fake = install(three_projects())
    assert projects.resolve_projects(folder=FOLDER, label="team=nobody") == []
    assert fake.count(PROJECTS_READ) == 1


def test_an_empty_label_value_is_a_value_and_selects_the_projects_carrying_it():
    install(listing(page_rows(project(A, labels={"stage": ""}),
                              project(B, labels={"stage": "prod"}))))
    assert projects.resolve_projects(folder=FOLDER, label="stage=") == [A]


def test_two_pages_are_both_pages():
    """Every page is read, so the scope is not cut short at the first page."""
    fake = install(listing(
        page_rows(project(A, labels={"team": "genomics"}), nextPageToken="TOKEN-2"),
        page_rows(project(B, labels={"team": "genomics"}))))
    assert projects.resolve_projects(folder=FOLDER, label="team=genomics") == [A, B]
    assert fake.count(PROJECTS_READ) == 2
    assert fake.queries(PROJECTS_READ)[1]["pageToken"] == "TOKEN-2"


def test_a_row_that_named_no_state_is_not_counted_as_active():
    """A row with no `state` is not ACTIVE, and a row with no `projectId` has no id. Neither
    is returned."""
    install(listing(page_rows({"state": "ACTIVE"},
                              {"projectId": B, "name": "projects/415104041263"})))
    assert projects.resolve_projects(folder=FOLDER) == []


def test_a_named_project_and_a_folder_are_the_union_of_both():
    """`-p X --folder 1234` means X plus the folder's projects. X is checked by its own `get`,
    since it need not be in the folder. The label filters only the folder's projects."""
    fake = install({PROJECTS_READ: [{"projects": [project(A, labels={"team": "genomics"}),
                                                   project(B, labels={"team": "imaging"})]}],
                    PROJECT_READ: [project(C, labels={"team": "genomics"})]})
    assert projects.resolve_projects(projects=[C], folder=FOLDER, label="team=genomics") == [A, C]
    assert fake.count(PROJECT_READ) == 1
    assert fake.count(PROJECTS_READ) == 1


def test_all_with_a_folder_is_that_folder_and_asks_once():
    """With a folder, `--all` adds no request. v3's list is not recursive, so the folder's one
    read already returns every ACTIVE child."""
    fake = install(listing(page_rows(project(A), project(B, state="DELETE_REQUESTED"))))
    assert projects.resolve_projects(folder=FOLDER, everything=True) == [A]
    assert fake.count(PROJECTS_READ) == 1
    assert fake.queries(PROJECTS_READ)[0]["parent"] == f"folders/{FOLDER}"


# --------------------------------------------------------------------- refusals

SEARCH_READ = "cloudresourcemanager.projects.search"


def searched() -> dict:
    """`projects.search` answering the same three projects as `three_projects()`."""
    return {SEARCH_READ: three_projects()[PROJECTS_READ]}


def test_all_searches_everything_visible_and_keeps_only_active_projects():
    fake = install(searched())
    assert projects.resolve_projects(everything=True) == [A, B]
    assert fake.count(SEARCH_READ) == 1 and fake.count(PROJECTS_READ) == 0
    assert fake.queries(SEARCH_READ) == [{"pageSize": PAGE_SIZE}]


def test_a_bare_label_sends_one_label_term_and_rechecks_the_label_on_every_row():
    """`query` ORs multiple terms and is case-insensitive, so only one term goes out and the exact
    label and ACTIVE state are re-checked here. C carries the label and is DELETE_REQUESTED."""
    fake = install(searched())
    assert projects.resolve_projects(label="team=genomics") == [A]
    assert fake.queries(SEARCH_READ) == [{"pageSize": PAGE_SIZE, "query": "labels.team:genomics"}]


def test_a_label_with_an_empty_value_searches_for_the_key_and_matches_the_empty_value():
    fake = install({SEARCH_READ: [{"projects": [project(A, labels={"tier": ""}),
                                                project(B, labels={"tier": "gold"})]}]})
    assert projects.resolve_projects(label="tier=") == [A]
    assert fake.queries(SEARCH_READ)[0]["query"] == "labels.tier:*"


def test_a_named_project_and_a_bare_label_are_a_union():
    fake = install({**searched(), PROJECT_READ: [project(B)]})
    assert projects.resolve_projects(projects=[B], label="team=genomics") == [A, B]
    assert fake.count(PROJECT_READ) == 1 and fake.count(SEARCH_READ) == 1


def test_a_refused_search_stops_the_run():
    install({}, failures={SEARCH_READ: (403, "denied")})
    with pytest.raises(guard.ReadFailed):
        projects.resolve_projects(everything=True)


@pytest.mark.parametrize("bad", ["12/34", "org", "1", "abc", "", " ",
                                 f"folders/{FOLDER}", f"{FOLDER} ", "1" * 31, "8765432;DELETE"])
def test_a_folder_id_that_is_not_4_to_30_digits_refuses_before_any_request(bad):
    """The guard checks query keys, not their values, so the folder id is validated here before
    any request."""
    fake = install(three_projects())
    with pytest.raises(guard.AuditError):
        projects.resolve_projects(folder=bad)
    assert fake.calls == []


@pytest.mark.parametrize("bad", ["Not A Label", "team", "team=gen ics", "TEAM=genomics",
                                 "team=genomics=extra", "=genomics", "team=UPPER", "1team=x",
                                 " team=x", "team=a" * 7])
def test_a_malformed_label_refuses_instead_of_answering_an_empty_list(bad):
    """An empty list means "looked, found nothing", so a malformed label must raise instead and
    send no request. The shape is checked first, so a typo is reported as a typo and not as a
    missing --folder."""
    fake = install(three_projects())
    with pytest.raises(guard.AuditError):
        projects.resolve_projects(label=bad)
    assert fake.calls == []


@pytest.mark.parametrize("bad", ["Lab-Seq", "ab", "a" * 31, "-lab", "lab-", "lab seq", "",
                                 "lab_seq", "lab.seq", "lab@seq"])
def test_an_id_that_is_not_a_project_id_refuses_rather_than_being_sent_anyway(bad):
    """The project id is validated here, before the `name` path value is built."""
    fake = install({**three_projects(), PROJECT_READ: [project(A)]})
    with pytest.raises(guard.AuditError):
        projects.resolve_projects(projects=[bad])
    assert fake.calls == []


def test_a_validating_caller_sends_the_name_it_validated():
    """For a valid id, the URL carries exactly `projects/<id>`."""
    fake = install({PROJECT_READ: [project(A)]})
    assert projects.resolve_projects(projects=[A]) == [A]
    assert fake.urls(PROJECT_READ) == [
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{A}"]


def test_the_read_count_is_the_number_the_run_prints():
    """The run prints the read count in `README.txt` via `layer1_line()`.

    `tests/test_methods.py` and `tests/test_cli_table.py` pin the same number elsewhere. The test
    name has no number, so it stays true when the allowlist grows."""
    assert methods.READ_COUNT == 20
    assert f"{methods.READ_COUNT} named reads" in guard.layer1_line()


def test_no_selector_at_all_refuses_rather_than_returning_an_empty_scope():
    fake = install(three_projects())
    with pytest.raises(guard.AuditError) as err:
        projects.resolve_projects()
    assert "no scope" in str(err.value)
    assert fake.calls == []


def test_a_refused_folder_read_is_a_stopped_run_and_not_a_list_of_zero_projects():
    """A failed folder read must not look like an empty scope. `ReadFailed` is an `AuditError`,
    so `cli.py` stops the run."""
    install(three_projects(), failures={PROJECTS_READ: (403, "PERMISSION_DENIED no access")})
    with pytest.raises(guard.ReadFailed):
        projects.resolve_projects(folder=FOLDER)


# -------------------------------------------------------------- enabled services

def enabled(project: str = A) -> tuple[list[str], str | None]:
    return projects.enabled_services(project)


def test_the_enabled_services_come_back_as_api_domains_sorted_once():
    install(service_list({"services": [service("compute.googleapis.com"),
                                       service("container.googleapis.com"),
                                       service("bigquery.googleapis.com")]}))
    names, reason = enabled()
    assert names == ["bigquery.googleapis.com", "compute.googleapis.com",
                     "container.googleapis.com"]
    assert reason is None


def test_a_disabled_service_is_not_reported_as_enabled_just_because_the_server_returned_it():
    """The request filters on `state:ENABLED`, but a row's own `state` still wins."""
    install(service_list({"services": [
        service("compute.googleapis.com"),
        service("androidmanagement.googleapis.com", "DISABLED"),
        service("run.googleapis.com", "DISABLED"),
    ]}))
    assert enabled() == (["compute.googleapis.com"], None)


def test_the_request_names_the_filter_and_the_parent_that_make_it_a_project_read():
    fake = install(service_list({"services": []}))
    enabled()
    assert fake.urls(SERVICES_READ) == [
        "https://serviceusage.googleapis.com/v1/projects/lab-alpha-seq/services"]
    assert fake.queries(SERVICES_READ)[0]["filter"] == "state:ENABLED"


def test_both_pages_of_the_service_list_are_read():
    fake = install(service_list(
        {"services": [service("compute.googleapis.com")], "nextPageToken": "PAGE-2"},
        {"services": [service("sqladmin.googleapis.com")]}))
    assert enabled() == (["compute.googleapis.com", "sqladmin.googleapis.com"], None)
    assert fake.count(SERVICES_READ) == 2
    assert fake.queries(SERVICES_READ)[1]["pageToken"] == "PAGE-2"


def test_config_names_the_service_when_the_consumer_path_does_not():
    install(service_list({"services": [{"state": "ENABLED",
                                        "config": {"name": "pubsub.googleapis.com"}}]}))
    assert enabled() == (["pubsub.googleapis.com"], None)


def test_a_row_that_names_nothing_is_said_loudly_rather_than_dropped_off_the_list():
    """The enabled list shows what the table does not cover, so a row with no name is reported
    in the reason rather than silently dropped."""
    rows = [service("compute.googleapis.com"), {"state": "ENABLED"}]
    install(service_list({"services": rows}))
    names, reason = enabled()
    assert names == ["compute.googleapis.com"]
    assert reason and "no name" in reason


def test_a_refused_services_read_says_we_do_not_know_rather_than_nothing_is_on():
    """After a 403 the list is unknown, not empty, so a reason is returned. `cli.py` turns it into
    a gap line."""
    install(service_list({"services": [service("compute.googleapis.com")]}),
          failures={SERVICES_READ: (403, "PERMISSION_DENIED serviceusage is not granted")})
    names, reason = enabled()
    assert names == []
    assert reason is not None
    assert "403" in reason and SERVICES_READ in reason


def test_a_service_read_that_ran_out_of_its_page_cap_keeps_what_it_saw_and_says_it_is_short(
        monkeypatch):
    """When `guard.paged` hits its cap, the partial list is kept and the reason says it is
    incomplete."""
    install(service_list({"services": [service("compute.googleapis.com")],
                          "nextPageToken": "AGAIN"},
                         {"services": [service("run.googleapis.com")],
                          "nextPageToken": "AGAIN"}))
    real = guard.paged
    monkeypatch.setattr(guard, "paged",
                        lambda method_id, **kw: real(method_id, **{**kw, "max_pages": 1}))
    names, reason = enabled()
    assert names == ["compute.googleapis.com"]
    assert reason and "incomplete" in reason


def test_the_service_list_is_read_again_for_every_project_in_the_scope():
    """`cli.py` asks once per project. Nothing is cached, so each project gets its own page walk
    and its own enabled list."""
    fake = install(service_list(
        {"services": [service("compute.googleapis.com")], "nextPageToken": "A-2"},
        {"services": [service("run.googleapis.com")]},
        {"services": [service("bigquery.googleapis.com")], "nextPageToken": "B-2"},
        {"services": [service("sqladmin.googleapis.com")]}))
    assert enabled(A) == (["compute.googleapis.com", "run.googleapis.com"], None)
    assert enabled(B) == (["bigquery.googleapis.com", "sqladmin.googleapis.com"], None)
    assert fake.count(SERVICES_READ) == 4
    parents = [url.partition("/v1/")[2].removesuffix("/services") for url in
               fake.urls(SERVICES_READ)]
    assert parents == [f"projects/{A}", f"projects/{A}", f"projects/{B}", f"projects/{B}"]


def test_a_project_id_that_is_not_a_project_id_refuses_before_the_parent_is_built():
    """The project id is validated before `parent` is built, so a bad id raises a clear
    `AuditError` instead of a guard refusal inside the page loop."""
    fake = install(service_list({"services": []}))
    with pytest.raises(guard.AuditError):
        projects.enabled_services("Lab Seq")
    assert fake.calls == []
