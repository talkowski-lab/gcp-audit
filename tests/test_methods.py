"""Layer 2: the method allowlist refuses every request it does not name.

A hole here would let the toolkit change someone's cloud, so these tests matter most.
"""
from __future__ import annotations

import pathlib

import pytest

from gcp_audit import methods as M

GOOD = {
    "project": "lab-seq-prd", "projectId": "lab-seq-prd", "datasetId": "ds_1",
    "tableId": "variants_2026", "bucket": "lab-raw",
}


def params_for(read: M.Read) -> dict[str, str]:
    out = {}
    for name in read.path_params:
        if name == "parent":
            out[name] = ("services/6F81-5844-456A" if "cloudbilling" in read.method_id
                         else "projects/lab-seq-prd")
        elif name == "name":
            out[name] = "projects/lab-seq-prd"
        else:
            out[name] = GOOD[name]
    return out


# ---------------------------------------------------------------- the count and the shape

def test_the_page_has_the_number_of_reads_it_claims():
    """Adding a read must be deliberate, so the count is pinned and a new read breaks this test.

    The run's printed count is pinned in `tests/test_projects.py` and the `guard-check` count in
    `tests/test_cli_table.py`.
    """
    assert M.READ_COUNT == 20
    assert len(M.READS) == M.READ_COUNT


def test_the_prose_that_counts_the_allowlist_says_what_the_allowlist_says():
    """Prose that states the read count must match `READ_COUNT`.

    The first dict lists files that state the count in words, with every older count they have
    used. Widening the allowlist breaks this test until those sentences are updated. The second
    list holds files that must state no count at all, so they cannot go stale."""
    root = pathlib.Path(M.__file__).resolve().parents[2]
    says = {"docs/SAFETY.md": ("twenty requests", ("seventeen requests", "sixteen requests", "fourteen requests")),
            "README.md": ("twenty named requests",
                          ("seventeen named requests", "sixteen named requests", "fifteen named requests")),
            "tests/test_guard.py": ("twenty reads",
                                    ("seventeen reads", "sixteen reads", "fifteen reads", "fourteen reads"))}
    for rel, (current, stale) in says.items():
        text = (root / rel).read_text(encoding="utf-8")
        assert current in text, f"{rel} does not state the allowlist count as {current!r}"
        for word in stale:
            # Check every older count, so a revert to any of them is caught.
            assert word not in text, f"{rel} still says {word!r} while READ_COUNT is {M.READ_COUNT}"

    words = ("14 reads", "15 reads", "16 reads", "17 reads", "fourteen reads", "fifteen reads",
             "sixteen reads", "seventeen reads", "20 reads", "twenty reads")
    count_free = {rel: words for rel in ("src/gcp_audit/guard.py", "AGENTS.md", "CONTRIBUTING.md",
                                         "docs/DESIGN.md", "docs/SETUP.md",
                                         "docs/API-EVIDENCE.md")}
    for rel, forbidden in count_free.items():
        text = (root / rel).read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, (f"{rel} states {word!r}; it is a file that quotes no count, "
                                      "because prose that copies READ_COUNT is how the rulebook ends "
                                      "up contradicting the gate")


def test_every_read_builds_a_url_and_round_trips_through_match():
    for read in M.READS.values():
        url = read.url_of(params_for(read))
        assert url.startswith(f"https://{read.host}/"), read.method_id
        matched = M.match(read.verb, url)
        assert matched is not None, f"{read.method_id} builds a URL its own matcher rejects"
        assert matched.method_id == read.method_id


def test_every_read_declares_why_it_is_a_read_and_a_narrow_scope():
    """Every entry needs a stated reason and at least one admissible narrow scope."""
    for read in M.READS.values():
        assert len(read.why) > 40, f"{read.method_id} has no reason on the page"
        assert read.scopes, f"{read.method_id} declares no scope"
        for scope in read.scopes:
            assert scope in _scope_names(), f"{read.method_id} needs {scope}, which is not admissible"


def _scope_names() -> set[str]:
    """The scope allowlist holds full URLs; methods.py uses the short names."""
    from gcp_audit import guard
    return {s.rsplit("/", 1)[-1] for s in guard.SCOPE_ALLOWLIST}


def test_only_the_named_post_may_carry_a_body():
    posts = [r for r in M.READS.values() if r.verb == "POST"]
    assert [r.method_id for r in posts] == ["bigquery.jobs.query"]
    for read in M.READS.values():
        if read.verb != "POST":
            assert not read.body_params, f"{read.method_id} is a GET that accepts a body"


def test_no_read_is_a_write_shaped_method_id():
    """No method id ends in a write verb. The last segment is compared exactly, not by substring."""
    for method_id in M.READS:
        tail = method_id.split(".")[-1]
        assert tail not in {"create", "delete", "insert", "patch", "update", "set", "move",
                            "testIamPermissions", "start", "stop", "execute", "undelete"}, method_id


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("method_id", [
    "compute.instances.delete",
    "compute.instances.stop",
    "compute.instances.setLabels",
    "storage.objects.delete",
    "cloudbilling.billingAccounts.move",                 # GET and mutates, per its own document
    "storage.buckets.testIamPermissions",
    "cloudasset.resources.searchAll",                    # declares no read-only scope
    "container.projects.locations.clusters.list",        # needs a scope we do not ask for
    "resourcemanager.projects.setIamPolicy",
    "bigquery.tables.delete",
    "",
    "compute.instances.aggregatedList ".strip() + "x",
])
def test_a_method_not_on_the_page_is_refused(method_id):
    with pytest.raises(M.MethodNotAllowed):
        M.lookup(method_id)


@pytest.mark.parametrize("bad", ["../x", "lab..prd", "Lab-PRD", "lab prd", "lab-prd/", "a?b",
                                 "", "projects/lab-seq-prd"])
def test_a_placeholder_value_off_its_pattern_is_refused(bad):
    """`projects/...` is refused for `project` because a slash would let a value lengthen the
    path. The templates already spell out their prefixes."""
    with pytest.raises(M.MethodNotAllowed):
        M.lookup("compute.disks.aggregatedList").url_of({"project": bad})


def test_a_query_key_that_is_not_admitted_is_refused():
    read = M.lookup("compute.images.list")
    reason = read.admits(params={"project": "lab-seq-prd"}, query={"$select": "*"}, body=None)
    assert "query parameter" in reason


def test_the_folder_filter_is_admitted_and_the_deleted_project_filter_is_not():
    """`--folder` needs `parent=`. `showDeleted` would add DELETE_REQUESTED projects to the table,
    and the code never sends it."""
    read = M.lookup("cloudresourcemanager.projects.list")
    assert "parent" in read.query_params
    assert "showDeleted" not in read.query_params
    assert read.admits(params={}, query={"parent": "folders/123456789012"}, body=None) == ""
    assert "not admitted" in read.admits(params={}, query={"showDeleted": "true"}, body=None)


def test_the_project_get_takes_a_project_name_and_nothing_else():
    """`projects.get` takes one path value and no query keys.

    `name` uses the `project_parent` pattern, which is narrower than the API's `^projects/[^/]+$`.
    It admits only the shapes this toolkit builds.
    """
    read = M.lookup("cloudresourcemanager.projects.get")
    assert read.url_of({"name": "projects/lab-seq-prd"}) == (
        "https://cloudresourcemanager.googleapis.com/v3/projects/lab-seq-prd")
    assert read.query_params == frozenset()


@pytest.mark.parametrize("bad", ["folders/123456789012", "organizations/123456789012",
                                "projects/Lab-Seq", "projects/lab seq", "projects/lab_seq",
                                "projects/", "projects/lab/seq", "lab-seq-prd", "", "projects/x"])
def test_a_projects_get_name_that_is_not_projects_slash_a_project_id_is_refused(bad):
    """`projects/` alone would build `v3/projects/`, which no read uses. A bare id, from a caller
    that skipped `projects._project_name`, is refused here too."""
    with pytest.raises(M.MethodNotAllowed):
        M.lookup("cloudresourcemanager.projects.get").url_of({"name": bad})


def test_the_parent_the_list_needs_is_not_admitted_on_the_get():
    """`parent` selects the list's collection and has no meaning on `get`, so `get` refuses it."""
    read = M.lookup("cloudresourcemanager.projects.get")
    assert "not admitted" in read.admits(params={"name": "projects/lab-seq-prd"},
                                        query={"parent": "folders/123456789012"}, body=None)
    assert read.admits(params={"name": "projects/lab-seq-prd"},
                       query={"pageSize": 200}, body=None) == ""


def test_the_two_resource_manager_paths_do_not_shadow_each_other():
    """`v3/projects` is the collection and `v3/projects/<id>` is one member. Each URL must match
    only its own read."""
    host = "https://cloudresourcemanager.googleapis.com"
    assert M.match("GET", f"{host}/v3/projects") is M.READS["cloudresourcemanager.projects.list"]
    assert M.match("GET", f"{host}/v3/projects/lab-seq-prd") is (
        M.READS["cloudresourcemanager.projects.get"])
    assert M.match("GET", f"{host}/v3/projects/lab-seq-prd/") is None


def test_paging_keys_are_admitted_anywhere_because_a_page_loop_needs_them():
    read = M.lookup("compute.images.list")
    assert read.admits(params={"project": "lab-seq-prd"},
                       query={"maxResults": 500, "pageToken": "CAES..."}, body=None) == ""


def test_a_body_on_a_get_is_refused():
    reason = M.lookup("storage.buckets.list").admits(
        params={}, query={"project": "lab-seq-prd"}, body={"query": "SELECT 1"})
    assert "no body" in reason


def test_the_post_admits_only_its_listed_body_keys():
    read = M.lookup("bigquery.jobs.query")
    assert read.admits(params={"projectId": "lab-seq-prd"}, query=None,
                       body={"query": "SELECT x", "useLegacySql": False}) == ""
    assert "dryRun" in read.body_params
    assert "destinationTable" not in read.body_params
    assert "body key" in read.admits(params={"projectId": "lab-seq-prd"}, query=None,
                                     body={"destinationTable": {"tableId": "t"}})


# ---------------------------------------------------------------- URL matching

def test_a_url_nobody_described_is_refused():
    assert M.match("GET", "https://compute.googleapis.com/compute/v1/projects/p/instances/x") is None
    assert M.match("GET", "https://iam.googleapis.com/v1/projects/p/serviceAccounts") is None
    assert M.match("DELETE", "https://storage.googleapis.com/storage/v1/b/lab-raw") is None


def test_the_verb_is_part_of_the_match():
    """`.../queries` is a POST. A GET on the same path is not admitted."""
    url = "https://bigquery.googleapis.com/bigquery/v2/projects/lab-seq-prd/queries"
    assert M.match("POST", url) is not None
    assert M.match("GET", url) is None


def test_a_host_can_only_be_reached_by_its_own_reads():
    """storage.buckets.list cannot be pointed at the compute host, or vice versa."""
    assert M.match("GET", "https://compute.googleapis.com/storage/v1/b") is None
    assert M.match("GET", "https://storage.googleapis.com/compute/v1/projects/p/aggregated/disks") is None


def test_an_empty_placeholder_cannot_produce_a_double_slash_path():
    with pytest.raises(M.MethodNotAllowed):
        M.lookup("bigquery.tables.list").url_of({"projectId": "lab-seq-prd", "datasetId": ""})


# ------------------------------------------------ storage.objects.list: one page of one bucket

def test_the_object_listing_read_builds_the_document_s_path_and_no_other():
    """`objects.list` is `GET b/{bucket}/o` (`docs/API-EVIDENCE.md` §`objects.list`). It must not
    match a single object's path."""
    read = M.lookup("storage.objects.list")
    assert read.verb == "GET"
    assert read.url_of({"bucket": "dataproc-temp-us-central1-000000000000-abcde"}) == (
        "https://storage.googleapis.com/storage/v1/b/"
        "dataproc-temp-us-central1-000000000000-abcde/o")
    assert M.match("GET", read.url_of({"bucket": "lab-raw"})) is read
    assert M.match("GET", "https://storage.googleapis.com/storage/v1/b/lab-raw/o/") is None
    assert M.match("GET", "https://storage.googleapis.com/storage/v1/b/lab-raw/o/one.txt") is None
    assert M.match("DELETE", read.url_of({"bucket": "lab-raw"})) is None
    assert M.match("GET", "https://storage.googleapis.com/storage/v1/b") \
        is M.READS["storage.buckets.list"]
    assert M.match("GET", "https://storage.googleapis.com/storage/v1/b/lab-raw") is None


def test_the_object_listing_grants_two_pagination_keys_and_refuses_everything_that_edits_the_question():
    """An empty first page must mean "no live objects". `versions` would change what the listing
    sees, and `prefix`, `delimiter`, `filter` and the rest narrow it, so the guard refuses them."""
    read = M.lookup("storage.objects.list")
    assert read.admits(params={"bucket": "lab-raw"},
                       query={"maxResults": 1, "pageToken": "CAISAggB"}, body=None) == ""
    for refused in ("versions", "prefix", "delimiter", "startOffset", "endOffset", "filter",
                    "matchGlob", "softDeleted", "projection", "userProject"):
        assert "not admitted" in read.admits(params={"bucket": "lab-raw"},
                                            query={refused: "x"}, body=None), refused


def test_the_object_listing_is_two_of_three_reads_on_the_storage_host():
    """The storage host has exactly two reads: list buckets and list one bucket's objects."""
    storage = [r.method_id for r in M.READS.values() if r.host == "storage.googleapis.com"]
    assert storage == ["storage.buckets.list", "storage.objects.list"]
