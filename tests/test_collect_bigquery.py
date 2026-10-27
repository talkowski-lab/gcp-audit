"""collect_bigquery: tables sized by `tables.get` (default) or the TABLE_STORAGE query
(`--read-bq-storage`), and what each row says when a read did not answer.

Payload shapes follow `docs/API-EVIDENCE.md` §`tables.get` and
§`INFORMATION_SCHEMA.TABLE_STORAGE`.
"""
from __future__ import annotations

import pytest
from fake_session import FakeSession, install

from gcp_audit import collect_bigquery as cb
from gcp_audit import guard, model

PROJECT = "lab-prod-1"
GIB = model.GIB


@pytest.fixture(autouse=True)
def clean_guard():
    guard.reset()
    yield
    guard.reset()


class _OneDatasetRefused(FakeSession):
    """403 for one dataset's `tables.list` only; overrides `_dispatch` alone."""

    def __init__(self, routes: dict, poisoned: str) -> None:
        super().__init__(routes)
        self.poisoned = poisoned

    def _dispatch(self, method_id, verb, url, query, body):
        if self.poisoned in url:
            raise guard.ReadFailed(method_id, 403, "bigquery.tables.list denied on that dataset")
        return super()._dispatch(method_id, verb, url, query, body)


def dataset(dataset_id: str, location: str | None = "us-central1") -> dict:
    item: dict = {"kind": "bigquery#dataset", "id": f"{PROJECT}:{dataset_id}",
                  "datasetReference": {"datasetId": dataset_id, "projectId": PROJECT}}
    if location is not None:
        item["location"] = location
    return item


def table(dataset_id: str, table_id: str, **over: object) -> dict:
    item: dict = {"kind": "bigquery#table", "id": f"{PROJECT}:{dataset_id}:{table_id}",
                  "type": "TABLE",
                  "tableReference": {"projectId": PROJECT, "datasetId": dataset_id,
                                     "tableId": table_id},
                  "creationTime": "1700000000000"}
    item.update(over)
    return item


def table_meta(dataset_id: str, table_id: str, num_bytes: str | None = str(5 * GIB),
               **over: object) -> dict:
    """`tables.get`. The default 5 GiB is staged by no view fixture, so it identifies the read."""
    item: dict = {"kind": "bigquery#table",
                  "tableReference": {"projectId": PROJECT, "datasetId": dataset_id,
                                     "tableId": table_id}}
    if num_bytes is not None:
        item["numBytes"] = num_bytes
    item.update(over)
    return item


def view_row(dataset_id: str, table_id: str, total: str | None = str(10 * GIB),
             active: str | None = str(6 * GIB), long_term: str | None = str(4 * GIB),
             modified: str | None = None) -> tuple:
    return (dataset_id, table_id, total, active, long_term, modified)


def storage_result(*rows: tuple, **over: object) -> dict:
    payload: dict = {
        "kind": "bigquery#queryResponse", "jobComplete": True,
        "jobReference": {"projectId": PROJECT, "jobId": "job-1"},
        "totalRows": str(len(rows)),
        "schema": {"fields": [{"name": name} for name in cb.TABLE_STORAGE_COLUMNS]},
        "rows": [{"f": [{"v": v} for v in cells]} for cells in rows],
    }
    payload.update(over)
    return payload


def rows_of(found: model.Collect) -> dict[tuple[str, str], model.Resource]:
    return {(str(row.extra.get("dataset") or ""), row.name): row for row in found.resources}


def queries(fake: FakeSession) -> list[str]:
    return [b["query"] for mid, _u, _q, b in fake.calls if mid == cb.READ_QUERY]


def gaps_of(found: model.Collect) -> list[tuple[str, str]]:
    return [(gap.read, gap.reason) for gap in found.gaps]


def spend(count: int) -> None:
    """Use up requests through the real admission path, so `guard.stats()` reflects them."""
    for _ in range(count):
        guard.session().request(cb.READ_DATASETS, {"projectId": PROJECT})


# ---------------------------------------------------------------- rows


def test_each_table_is_a_row_naming_its_dataset_and_the_dataset_is_not_a_row():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", "events"),
                                                        table("ds_raw", "sessions")]},
                    "bigquery.tables.get": [table_meta("ds_raw", "events"),
                                            table_meta("ds_raw", "sessions")]})
    found = cb.collect_bigquery(PROJECT)

    assert [(row.kind, row.name) for row in found.resources] == [("Table", "events"),
                                                                 ("Table", "sessions")]
    for row in found.resources:
        assert row.extra["dataset"] == "ds_raw" and row.location == "us-central1"
        assert (row.last_activity, row.activity_field) == ("2023-11-14", "creationTime")
    assert fake.count("bigquery.datasets.list") == 1 and found.gaps == []


def test_an_empty_dataset_costs_no_query():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
                    "bigquery.tables.list": {"tables": []},
                    "bigquery.jobs.query": storage_result()})
    assert cb.collect_bigquery(PROJECT, read_storage_info=True).resources == []
    assert fake.count("bigquery.jobs.query") == 0


# ------------------------------------------------- default pass: tables.get


def test_a_default_pass_sizes_every_table_by_metadata_and_never_queries():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US"),
                                                            dataset("ds_raw", "us-central1")]},
                    "bigquery.tables.list": [{"tables": [table("ds_cold", "frozen")]},
                                             {"tables": [table("ds_raw", "events")]}],
                    "bigquery.tables.get": [table_meta("ds_cold", "frozen"),
                                            table_meta("ds_raw", "events")]})
    found = cb.collect_bigquery(PROJECT)

    assert fake.count("bigquery.jobs.query") == 0
    assert fake.count("bigquery.tables.get") == 2
    for row in found.resources:
        assert row.size_bytes == 5 * GIB and row.size_source == "bigquery.tables.get/numBytes"
        # Sized, but notes that the read that would date the last write was not sent.
        assert row.extra["not_measured"] == cb.NOT_READ
    assert found.gaps == []


def test_the_age_halves_are_kept_when_they_add_to_the_size():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_cold", "frozen")]},
             "bigquery.tables.get": table_meta("ds_cold", "frozen", str(5 * GIB),
                                               numActiveLogicalBytes=str(3 * GIB),
                                               numLongTermLogicalBytes=str(2 * GIB))})
    row = cb.collect_bigquery(PROJECT).resources[0]

    assert (row.extra["active_logical_bytes"], row.extra["long_term_logical_bytes"]) == (
        3 * GIB, 2 * GIB)
    evidence = model.cells(model.Row.of(row, None, None))[-1]
    assert "active_gib=3.0" in evidence and "long_term_gib=2.0" in evidence


def test_halves_that_do_not_add_up_are_reported_and_neither_is_kept():
    found_payload = table_meta("ds_cold", "frozen", str(5 * GIB),
                               numActiveLogicalBytes=str(5 * GIB),
                               numLongTermLogicalBytes=str(GIB),
                               numTotalLogicalBytes=str(6 * GIB))
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_cold", "frozen")]},
             "bigquery.tables.get": found_payload})
    found = cb.collect_bigquery(PROJECT)
    row = found.resources[0]

    assert "active_logical_bytes" not in row.extra and "long_term_logical_bytes" not in row.extra
    assert row.extra["split_disagrees"] == (
        f"active {5 * GIB} + long-term {GIB} != numBytes (numTotalLogicalBytes {6 * GIB}) {5 * GIB}")
    assert row.size_bytes == 5 * GIB
    assert any("byte split refused, neither tier taken" in g.reason for g in found.gaps)


def test_absent_halves_stay_unobserved_and_a_zero_half_is_an_answer():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_cold", "a"), table("ds_cold", "b")]},
             "bigquery.tables.get": [table_meta("ds_cold", "a"),
                                     table_meta("ds_cold", "b", str(5 * GIB),
                                                numActiveLogicalBytes=str(5 * GIB),
                                                numLongTermLogicalBytes="0")]})
    rows = rows_of(cb.collect_bigquery(PROJECT))

    assert "active_logical_bytes" not in rows[("ds_cold", "a")].extra
    assert rows[("ds_cold", "b")].extra["long_term_logical_bytes"] == 0
    assert "long_term_gib=0.0" in model.cells(model.Row.of(rows[("ds_cold", "b")], None, None))[-1]


def test_a_metadata_answer_with_no_bytes_is_unsized_not_zero():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_cold", "frozen")]},
             "bigquery.tables.get": table_meta("ds_cold", "frozen", num_bytes=None)})
    row = cb.collect_bigquery(PROJECT).resources[0]
    assert row.size_bytes is None and row.extra["not_measured"] == cb.NO_BYTES_GET


def test_a_metadata_answer_about_another_table_is_not_taken():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_cold", "frozen")]},
             "bigquery.tables.get": table_meta("ds_cold", "something_else")})
    found = cb.collect_bigquery(PROJECT)

    assert found.resources[0].size_bytes is None
    assert ("bigquery.tables.get",
            "metadata answered for another table, so no size was taken: asked ds_cold/frozen, "
            "answered ds_cold/something_else") in gaps_of(found)


def test_a_refused_metadata_read_is_one_gap_per_table():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw", "US")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events"),
                                                 table("ds_raw", "sessions")]}},
            failures={"bigquery.tables.get": (403, "bigquery.dataViewer denied")})
    found = cb.collect_bigquery(PROJECT)

    assert [row.size_bytes for row in found.resources] == [None, None]
    assert [row.extra["not_measured"] for row in found.resources] == [cb.NOT_READ] * 2
    refused = [gap.reason for gap in found.gaps if gap.read == cb.READ_TABLE]
    assert refused == [f"metadata read refused, no size taken: bigquery.dataViewer denied "
                       f"[ds_raw/{name}]" for name in ("events", "sessions")]


def test_a_table_name_the_allowlist_cannot_address_costs_one_row_not_the_run():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
                    "bigquery.tables.list": {"tables": [
                        table("ds_cold", "évènements"),
                        table("ds_cold", "frozen"),
                        table("ds_cold", "no-id", tableReference={"projectId": PROJECT,
                                                                  "datasetId": "ds_cold"})]},
                    "bigquery.tables.get": table_meta("ds_cold", "frozen")})
    found = cb.collect_bigquery(PROJECT)

    assert fake.count("bigquery.tables.get") == 1
    by_name = {row.name: row for row in found.resources}
    assert by_name["frozen"].size_bytes == 5 * GIB
    assert by_name["évènements"].size_bytes is None
    # No tableId: the row prints NA, and since `NA` is a valid id nothing is sent for it.
    assert by_name[model.UNKNOWN].size_bytes is None
    assert by_name[model.UNKNOWN].extra["not_measured"] == cb.NO_TABLE_ID
    reasons = [gap.reason for gap in found.gaps if gap.read == cb.READ_TABLE]
    assert reasons == ["ds_cold/évènements: bigquery.tables.get: tableId='évènements' "
                       "is not shaped like a tableId id"]


def test_a_table_or_dataset_with_no_id_is_never_addressed_in_either_mode():
    """`NA` is a valid BigQuery id, so a missing one must not become a request for a real `NA`."""
    no_id = table("ds_raw", "x", tableReference={"projectId": PROJECT, "datasetId": "ds_raw"})
    for flag in (False, True):
        guard.reset()
        fake = install({"bigquery.datasets.list": {"datasets": [
                            dataset("ds_raw"), {"kind": "bigquery#dataset", "location": "US"}]},
                        "bigquery.tables.list": {"tables": [no_id]},
                        "bigquery.jobs.query": storage_result(view_row("ds_raw", model.UNKNOWN))})
        found = cb.collect_bigquery(PROJECT, read_storage_info=flag)
        row = found.resources[0]
        assert fake.count("bigquery.tables.get") == 0 and fake.count("bigquery.tables.list") == 1
        assert row.size_bytes is None and row.extra["not_measured"] == cb.NO_TABLE_ID
        assert any("no datasetId" in g.reason for g in found.gaps)


def test_the_metadata_reads_give_up_after_three_refusals_and_tally_the_rest():
    fake = install(
        {"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
         "bigquery.tables.list": {"tables": [table("ds_cold", n) for n in "abcd"]}},
        failures={"bigquery.tables.get": (403, "bigquery.dataViewer denied")})
    found = cb.collect_bigquery(PROJECT)

    assert fake.count("bigquery.tables.get") == cb.MAX_GET_REFUSALS
    tally = found.gaps[-1].reason
    assert tally == ("gave up after 3 refusals; 1 more table was never asked about, so it is "
                     "unsized, not measured-empty")
    assert rows_of(found)[("ds_cold", "d")].extra["not_measured"] == cb.NOT_READ


@pytest.mark.parametrize("spent,asks,tally", [
    (5990, 4, None),
    (5992, 3, "request budget spent after 3 reads; 1 more table was never asked about, so it is "
              "unsized, not measured-empty"),
    (5996, 1, "request budget spent after 1 read; 3 more tables were never asked about, so they "
              "are unsized, not measured-empty"),
])
def test_the_request_budget_bounds_the_metadata_reads(spent: int, asks: int, tally: str | None):
    """The metadata reads get half of the remaining request budget. The datasets and tables reads
    use two requests before them."""
    names = ("t1", "t2", "t3", "t4")
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_cold", "US")]},
                    "bigquery.tables.list": {"tables": [table("ds_cold", n) for n in names]},
                    "bigquery.tables.get": [table_meta("ds_cold", n, str(GIB * (i + 1)))
                                            for i, n in enumerate(names)]})
    spend(spent)
    found = cb.collect_bigquery(PROJECT)

    assert fake.count("bigquery.tables.get") == asks
    assert [row.size_bytes for row in found.resources] == (
        [GIB * (i + 1) for i in range(asks)] + [None] * (4 - asks))
    assert [row.extra["not_measured"] for row in found.resources] == (
        [cb.NOT_READ] * asks + [cb.NOT_ASKED_BUDGET] * (4 - asks))
    assert [gap.reason for gap in found.gaps] == ([tally] if tally else [])


def test_the_two_stopping_sentences_stay_distinct():
    assert cb.NOT_ASKED_BUDGET == "tables.get never asked (request budget spent)"
    assert cb.SIZED_BY_GET == frozenset({cb.NOT_READ, cb.LOCATION_UNREADABLE})


# ------------------------------------------------- --read-bq-storage: the query


def test_the_query_is_byte_for_byte_the_documented_shape():
    """`[PROJECT_ID.]region-REGION.INFORMATION_SCHEMA.TABLE_STORAGE`, columns from the schema page."""
    literal = ("SELECT table_schema, table_name, total_logical_bytes, active_logical_bytes, "
               "long_term_logical_bytes, storage_last_modified_time "
               "FROM `lab-prod-1`.`region-us-central1`.INFORMATION_SCHEMA.TABLE_STORAGE "
               "WHERE deleted IS NOT TRUE")
    assert cb.table_storage_sql(PROJECT, "us-central1") == literal

    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
                    "bigquery.jobs.query": storage_result()})
    cb.collect_bigquery(PROJECT, read_storage_info=True)
    sent = [b for mid, _u, _q, b in fake.calls if mid == cb.READ_QUERY][0]
    assert sent == {"query": literal, "useLegacySql": False, "maximumBytesBilled": "104857600"}


def test_one_query_per_location_and_a_multi_region_is_qualified_like_any_other():
    fake = install(
        {"bigquery.datasets.list": {"datasets": [dataset("ds_a", "us-central1"),
                                                 dataset("ds_b", "US-CENTRAL1"),
                                                 dataset("ds_c", "US"),
                                                 dataset("ds_d", "EU")]},
         "bigquery.tables.list": [{"tables": [table(d, "t")]} for d in
                                  ("ds_a", "ds_b", "ds_c", "ds_d")],
         "bigquery.jobs.query": storage_result()})
    cb.collect_bigquery(PROJECT, read_storage_info=True)

    sent = queries(fake)
    assert len(sent) == 3
    for location in ("region-eu", "region-us", "region-us-central1"):
        assert sum(f"`{location}`" in q for q in sent) == 1


@pytest.mark.parametrize("bad", ["lab-prod-1; DROP", "pro`ject", "a", "", "UPPER_CASE"])
def test_a_project_that_is_not_an_id_never_reaches_the_query(bad: str):
    with pytest.raises(guard.AuditError):
        cb.table_storage_sql(bad, "us-central1")


@pytest.mark.parametrize("bad", ["US`", "us-central1; SELECT", "US EU", "eu.west", "", "EU'"])
def test_a_location_that_is_not_an_id_never_reaches_the_query(bad: str):
    with pytest.raises(guard.AuditError):
        cb.table_storage_sql(PROJECT, bad)


@pytest.mark.parametrize("value", [None, "", "us central1", "US; DROP TABLE x"])
def test_an_unreadable_location_is_never_queried_and_is_sized_by_metadata(value):
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw", value)]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
                    "bigquery.jobs.query": storage_result(),
                    "bigquery.tables.get": table_meta("ds_raw", "events")})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert fake.count("bigquery.jobs.query") == 0
    assert gaps_of(found) == [(cb.READ_QUERY, cb.LOCATION_UNREADABLE + ": ds_raw")]
    row = found.resources[0]
    assert row.size_bytes == 5 * GIB and row.extra["not_measured"] == cb.LOCATION_UNREADABLE


def test_an_unreadable_location_is_not_a_gap_when_no_query_was_authorised():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw", None)]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
             "bigquery.tables.get": table_meta("ds_raw", "events")})
    assert cb.collect_bigquery(PROJECT).gaps == []


def test_the_view_sizes_splits_and_dates_a_table():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw", "US")]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
                    "bigquery.jobs.query": storage_result(
                        view_row("ds_raw", "events", modified="2026-09-20T01:02:03.456Z"))})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)
    row = found.resources[0]

    assert fake.count("bigquery.tables.get") == 0
    assert row.size_bytes == 10 * GIB and row.size_source == "TABLE_STORAGE"
    assert (row.extra["active_logical_bytes"], row.extra["long_term_logical_bytes"]) == (
        6 * GIB, 4 * GIB)
    assert (row.last_activity, row.activity_field) == (
        "2026-09-20", "TABLE_STORAGE.storage_last_modified_time")
    assert "not_measured" not in row.extra and found.gaps == []


def test_a_view_split_that_does_not_add_up_is_refused():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
             "bigquery.jobs.query": storage_result(
                 view_row("ds_raw", "events", active=str(9 * GIB)))})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert found.resources[0].extra["split_disagrees"] == (
        f"active {9 * GIB} + long-term {4 * GIB} != total_logical_bytes {10 * GIB}")
    assert found.gaps[0].read == cb.READ_QUERY


def test_a_view_row_is_joined_on_dataset_and_table_not_table_alone():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw"), dataset("ds_vault")]},
             "bigquery.tables.list": [{"tables": [table("ds_raw", "events")]},
                                      {"tables": [table("ds_vault", "events")]}],
             "bigquery.jobs.query": storage_result(
                 view_row("ds_raw", "events", str(10 * GIB), None, None),
                 view_row("ds_vault", "events", str(20 * GIB), None, None))})
    rows = rows_of(cb.collect_bigquery(PROJECT, read_storage_info=True))

    assert rows[("ds_raw", "events")].size_bytes == 10 * GIB
    assert rows[("ds_vault", "events")].size_bytes == 20 * GIB


def test_one_table_reported_twice_is_not_resolved_by_guessing():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
             "bigquery.jobs.query": storage_result(view_row("ds_raw", "events"),
                                                   view_row("ds_raw", "events", str(GIB)))})
    row = cb.collect_bigquery(PROJECT, read_storage_info=True).resources[0]
    assert row.size_bytes is None and row.extra["not_measured"] == cb.NAME_AMBIGUOUS


def test_an_unparsable_date_is_named_and_the_row_keeps_creationtime():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
             "bigquery.jobs.query": storage_result(
                 view_row("ds_raw", "events", modified="not-a-date"))})
    row = cb.collect_bigquery(PROJECT, read_storage_info=True).resources[0]

    assert (row.last_activity, row.activity_field) == ("2023-11-14", "creationTime")
    assert row.extra["date_unparsed"] == "storage_last_modified_time not-a-date"


def test_a_view_row_with_no_byte_count_is_unsized_not_zero():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "daily_view", type="VIEW")]},
             "bigquery.jobs.query": storage_result(
                 view_row("ds_raw", "daily_view", None, None, None))})
    row = cb.collect_bigquery(PROJECT, read_storage_info=True).resources[0]
    assert row.size_bytes is None and row.extra["not_measured"] == cb.NO_BYTES


def test_a_table_the_view_did_not_list_is_unsized_and_not_chased_per_table():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
                    "bigquery.jobs.query": storage_result()})
    row = cb.collect_bigquery(PROJECT, read_storage_info=True).resources[0]
    assert row.extra["not_measured"] == cb.NO_BYTES and fake.count("bigquery.tables.get") == 0


def test_a_query_over_the_byte_cap_fails_as_a_gap_and_costs_nothing():
    """BigQuery fails a job that would bill past `maximumBytesBilled`. That is a refused read."""
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]}},
            failures={"bigquery.jobs.query": (400, "bytesBilledLimitExceeded")})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)
    assert gaps_of(found) == [(cb.READ_QUERY, "bytesBilledLimitExceeded")]
    assert found.resources[0].extra["not_measured"] == cb.READ_REFUSED


def test_a_refused_query_is_a_gap_and_is_not_worked_around_per_table():
    fake = install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
                    "bigquery.tables.list": {"tables": [table("ds_raw", n)
                                                        for n in ("a", "b", "c", "d")]}},
                   failures={"bigquery.jobs.query": (403, "bigquery.jobs.create denied")})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert gaps_of(found) == [(cb.READ_QUERY, "bigquery.jobs.create denied")]
    assert fake.count("bigquery.tables.get") == 0
    assert [row.extra["not_measured"] for row in found.resources] == [cb.READ_REFUSED] * 4
    assert {row.last_activity for row in found.resources} == {"2023-11-14"}


@pytest.mark.parametrize("payload,reason", [
    (storage_result(jobComplete=False), "job did not complete, so no rows were read"),
    ({"jobComplete": True, "schema": {"fields": [{"name": "x"}]}, "rows": [{"f": [{"v": "1"}]}]},
     "answered without the named columns"),
])
def test_an_answer_that_cannot_be_read_is_a_gap_not_an_empty_view(payload: dict, reason: str):
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
             "bigquery.jobs.query": payload})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert found.gaps[0].reason == reason
    assert found.resources[0].extra["not_measured"] == cb.READ_REFUSED


def test_an_outstanding_page_token_uses_what_answered_and_names_the_rest():
    install({"bigquery.datasets.list": {"datasets": [dataset("ds_raw")]},
             "bigquery.tables.list": {"tables": [table("ds_raw", "events"),
                                                 table("ds_raw", "sessions")]},
             "bigquery.jobs.query": storage_result(view_row("ds_raw", "events"),
                                                   pageToken="CAAQAg")})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)
    rows = rows_of(found)

    assert "page token" in found.gaps[0].reason
    assert rows[("ds_raw", "events")].size_bytes == 10 * GIB
    assert rows[("ds_raw", "sessions")].extra["not_measured"] == cb.TRUNCATED


# ---------------------------------------------------------------- enumeration failures


def test_a_refused_dataset_read_is_a_gap_and_not_a_crash():
    fake = install({}, failures={"bigquery.datasets.list": (403, "denied")})
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert found.resources == [] and [g.read for g in found.gaps] == [cb.READ_DATASETS]
    assert fake.count("bigquery.tables.list") == 0


def test_a_refused_tables_read_silences_one_dataset_and_not_the_others():
    fake = _OneDatasetRefused({"bigquery.datasets.list": {"datasets": [dataset("ds_vault"),
                                                                       dataset("ds_raw")]},
                               "bigquery.tables.list": {"tables": [table("ds_raw", "events")]},
                               "bigquery.jobs.query": storage_result(
                                   view_row("ds_raw", "events"))},
                              poisoned="/datasets/ds_vault/tables")
    guard.install(fake)
    found = cb.collect_bigquery(PROJECT, read_storage_info=True)

    assert [row.name for row in found.resources] == ["events"]
    assert gaps_of(found) == [(cb.READ_TABLES,
                               "ds_vault: bigquery.tables.list denied on that dataset")]
    assert found.resources[0].size_bytes == 10 * GIB


def test_unreachable_locations_in_the_dataset_list_are_a_gap():
    install({"bigquery.datasets.list": {"datasets": [], "unreachable": ["europe-west5"]}})
    found = cb.collect_bigquery(PROJECT)
    assert found.gaps[0].read == cb.READ_DATASETS and "europe-west5" in found.gaps[0].reason


def test_an_empty_dataset_list_is_an_answer_not_a_gap():
    found = (install({"bigquery.datasets.list": {}}), cb.collect_bigquery(PROJECT))[1]
    assert found.resources == [] and found.gaps == []
