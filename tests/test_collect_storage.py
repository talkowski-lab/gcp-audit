"""collect_storage: buckets from `storage.buckets.list`, bytes from one gauge read, and one
object page for a bucket the gauge said nothing about.

Payloads follow `docs/API-EVIDENCE.md` §Cloud Storage. A size has four states that must stay
distinct: measured by the gauge, zero from the gauge, zero from an empty object page, and unknown.
The object-page zero applies only to an unversioned bucket whose one page came back empty and
complete.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fake_session import install

from gcp_audit import collect_storage as cs
from gcp_audit import guard, model, pricing

PROJECT = "lab-prod-1"


@pytest.fixture(autouse=True)
def clean_guard():
    guard.reset()
    yield
    guard.reset()


def bucket(name: str, **over: object) -> dict:
    """One `Bucket` as `buckets.list` returns it."""
    item: dict = {"kind": "storage#bucket", "id": name, "name": name,
                  "location": "us-central1", "locationType": "region",
                  "storageClass": "STANDARD",
                  "timeCreated": "2024-05-01T09:00:00Z",
                  "updated": "2026-09-25T04:20:00Z"}
    item.update(over)
    return item


def series(bucket_name: str | None, points: list[tuple[str, str]],
           **metric_labels: str) -> dict:
    """One `TimeSeries` of the byte gauge; `points` is (startTime, endTime, int64Value).

    Some tests pass points oldest first, because arrival order must not matter.
    """
    labels = {"project_id": PROJECT}
    if bucket_name is not None:
        labels["bucket_name"] = bucket_name
    return {"metric": {"type": cs.METRIC_TOTAL_BYTES,
                       "labels": dict(metric_labels) or {"location": "us-central1",
                                                         "type": "live-object"}},
            "resource": {"type": "gcs_bucket", "labels": labels},
            "metricKind": "GAUGE", "valueType": "INT64",
            "points": [{"interval": {"startTime": start, "endTime": finish},
                        "value": {"int64Value": value}}
                       for start, finish, value in points]}


def gauge(*series_list: dict) -> dict:
    return {"timeSeries": list(series_list)}


def rows_of(found: model.Collect) -> dict[str, model.Resource]:
    return {row.name: row for row in found.resources}


#: The default `storage.objects.list` page in this file: one object. A non-empty page does not
#: size a bucket, so gauge-silent buckets stay unsized unless a test stages its own page. Object
#: names are synthetic.
def listing(*names: str) -> dict:
    return {"items": [{"kind": "storage#object", "name": name} for name in names]}


LISTING = listing("sequence.txt")


def stage(routes: dict, failures: dict | None = None):
    """`fake_session.install` with the object listing already answered.

    A test can pass its own `storage.objects.list` route, which wins, or list it in `failures`."""
    return install({"storage.objects.list": LISTING, **routes}, failures)


# ------------------------------------------------------------------ the rows


def test_a_bucket_becomes_one_row_with_the_fields_buckets_list_gave():
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
                    "monitoring.projects.timeSeries.list": gauge()})
    found = cs.collect_storage(PROJECT)

    assert found.service == "storage.googleapis.com"
    assert found.gaps == []
    row = found.resources[0]
    assert (row.kind, row.name, row.location, row.state) == ("Bucket", "lab-raw-2021",
                                                             "us-central1", "")
    assert row.extra["storage_class"] == "STANDARD"
    assert fake.count("storage.buckets.list") == 1
    assert fake.urls("storage.buckets.list") == ["https://storage.googleapis.com/storage/v1/b"]
    assert fake.queries("storage.buckets.list") == [{"project": PROJECT}]


def test_a_nearline_bucket_names_its_class_because_the_rate_is_keyed_on_it():
    """Pricing is keyed on storage class, so each row carries the class the API reported."""
    stage({"storage.buckets.list": {"items": [bucket("lab-backup-nearline",
                                                       storageClass="NEARLINE")]},
             "monitoring.projects.timeSeries.list": gauge()})
    row = cs.collect_storage(PROJECT).resources[0]

    assert row.extra["storage_class"] == "NEARLINE"
    assert row.evidence()["storage_class"] == "NEARLINE"


def test_a_bucket_the_api_gave_no_class_for_carries_no_class_key():
    """With no class from the API, the key is absent, not empty. An empty `storage_class=` would
    read as an observed value."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021", storageClass=None),
                                                 bucket("lab-raw-2022", storageClass="")]},
             "monitoring.projects.timeSeries.list": gauge()})
    found = cs.collect_storage(PROJECT)

    assert len(found.resources) == 2
    for row in found.resources:
        assert "storage_class" not in row.extra
        assert "storage_class" not in row.evidence()


def test_the_date_comes_from_updated_and_named_as_updated_never_as_access():
    """`updated` is a metadata timestamp that changes on edits like a lifecycle rule, not on data
    access. The field name is printed with the date so it is not read as access."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021",
                                                       timeCreated="2021-01-01T00:00:00Z",
                                                       updated="2026-09-25T04:20:00Z")]},
             "monitoring.projects.timeSeries.list": gauge()})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.last_activity == "2026-09-25"
    assert row.activity_field == "updated"


def test_a_bucket_with_no_updated_is_a_row_with_no_date():
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021", updated=None)]},
             "monitoring.projects.timeSeries.list": gauge()})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.last_activity is None and row.activity_field is None
    cells = model.cells(model.Row.of(row, None, model.Unpriced("UNSIZED", "gauge silent")))
    assert cells[11] == "NA" and cells[12] == "not-read"


# ---------------------------------------------------------------- the gauge read


def test_the_gauge_is_one_read_for_the_project_with_filter_interval_and_view():
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
                    "monitoring.projects.timeSeries.list": gauge()})
    cs.collect_storage(PROJECT)

    assert fake.count("monitoring.projects.timeSeries.list") == 1
    assert fake.urls("monitoring.projects.timeSeries.list") == [
        f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/timeSeries"]
    query = fake.queries("monitoring.projects.timeSeries.list")[0]
    assert query["filter"] == 'metric.type="storage.googleapis.com/storage/total_bytes"'
    assert query["view"] == "FULL"
    start = datetime.strptime(query["interval.startTime"], "%Y-%m-%dT%H:%M:%SZ")
    end = datetime.strptime(query["interval.endTime"], "%Y-%m-%dT%H:%M:%SZ")
    naive_now = datetime.now(UTC).replace(tzinfo=None)
    assert (end - start) == cs.GAUGE_WINDOW
    assert end <= naive_now                       # `now`, not an invented date
    assert end >= naive_now - timedelta(minutes=5)


def test_the_gauge_window_is_at_least_a_week_because_the_gauge_samples_daily():
    """The byte gauge is sampled once a day with a delay. A short window can miss every point and
    report a sized bucket as unmeasured."""
    assert cs.GAUGE_WINDOW >= timedelta(days=7)


def test_a_matched_series_sizes_the_bucket_and_names_its_source():
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
             "monitoring.projects.timeSeries.list": gauge(
                 series("lab-raw-2021",
                        [("2026-09-18T05:03:00Z", "2026-09-19T05:03:00Z", "107374182400"),
                         ("2026-09-24T05:03:00Z", "2026-09-25T05:03:00Z", "128849018880")]))})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.size_bytes == 128849018880            # the newest point, not the first one
    assert row.size_source == cs.METRIC_TOTAL_BYTES
    assert row.extra.get("not_measured") is None
    assert row.evidence()["gib"] == 120.0


def test_a_multi_class_bucket_is_the_sum_of_its_series_not_one_series():
    """The gauge returns one series per label set, such as storage class, so a bucket's size is
    the sum of its series."""
    stage({"storage.buckets.list": {"items": [bucket("autoclass-photos")]},
             "monitoring.projects.timeSeries.list": gauge(
                 series("autoclass-photos",
                        [("2026-09-24T06:12:00Z", "2026-09-25T06:12:00Z", "42949672960")],
                        storage_class="STANDARD"),
                 series("autoclass-photos",
                        [("2026-09-23T06:12:00Z", "2026-09-24T06:12:00Z", "107374182400")],
                        storage_class="NEARLINE"),
                 series("autoclass-photos",
                        [("2026-09-23T07:41:00Z", "2026-09-24T07:41:00Z", "21474836480")],
                        storage_class="ARCHIVE"))})
    assert cs.collect_storage(PROJECT).resources[0].size_bytes == 171798691840


def test_a_series_that_names_no_bucket_is_nobody_s_size():
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
             "monitoring.projects.timeSeries.list": gauge(
                 series(None, [("2026-09-24T09:00:00Z", "2026-09-25T09:00:00Z", "999999")]))})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.size_bytes is None
    assert row.extra["not_measured"] == cs.NO_SERIES


def test_a_bucket_with_no_series_is_not_measured_not_zero():
    """The gauge emits nothing for a bucket it has not sampled, so no series is not zero. Here the
    object page finds an object, so the size stays unknown."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
             "monitoring.projects.timeSeries.list": gauge()})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.size_bytes is None
    assert row.extra["not_measured"] == cs.NO_SERIES
    assert "bytes" not in row.evidence()


def test_a_gauge_that_answered_zero_is_zero_not_a_question_mark():
    stage({"storage.buckets.list": {"items": [bucket("lab-empty-bucket")]},
             "monitoring.projects.timeSeries.list": gauge(
                 series("lab-empty-bucket",
                        [("2026-09-24T05:03:00Z", "2026-09-25T05:03:00Z", "0")]))})
    fake_rows = rows_of(cs.collect_storage(PROJECT))
    row = fake_rows["lab-empty-bucket"]
    assert row.size_bytes == 0
    assert row.extra.get("not_measured") is None
    assert row.evidence()["bytes"] == 0            # an observed zero is an answer


def test_a_point_whose_value_is_not_a_number_leaves_the_bucket_unmeasured():
    """A `distributionValue` is not a byte count, so it is not coerced into one."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]},
             "monitoring.projects.timeSeries.list":
                 gauge({"metric": {"type": cs.METRIC_TOTAL_BYTES},
                        "resource": {"type": "gcs_bucket",
                                     "labels": {"project_id": PROJECT,
                                                "bucket_name": "lab-raw-2021"}},
                        "metricKind": "GAUGE", "valueType": "INT64",
                        "points": [{"interval": {"endTime": "2026-09-25T05:03:00Z"},
                                    "value": {"distributionValue": {"count": "4"}}}]})})
    row = cs.collect_storage(PROJECT).resources[0]
    assert row.size_bytes is None
    assert row.extra["not_measured"] == cs.NO_SERIES


# -------------------------------------------------------------------- the gaps


def test_a_refused_buckets_list_is_a_gap_and_not_a_crash():
    fake = stage({"monitoring.projects.timeSeries.list": gauge()},
                   failures={"storage.buckets.list":
                             (403, "PERMISSION denied: storage.buckets.list")})
    found = cs.collect_storage(PROJECT)

    assert found.resources == []
    assert [g.read for g in found.gaps] == ["storage.buckets.list"]
    assert "PERMISSION denied" in found.gaps[0].reason
    assert found.gaps[0].project == PROJECT
    assert fake.count("monitoring.projects.timeSeries.list") == 0     # nothing to size


def test_a_refused_gauge_read_is_a_gap_and_the_rows_keep_no_size():
    """The rows survive without a size. `not_measured` says the read was refused, not that the
    gauge was silent, because the two have different fixes."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")]}},
            failures={"monitoring.projects.timeSeries.list": (403, "MONITORING.PERMISSION_DENIED")})
    found = cs.collect_storage(PROJECT)

    assert [(g.read, g.reason) for g in found.gaps] == [
        ("monitoring.projects.timeSeries.list", "MONITORING.PERMISSION_DENIED")]
    row = found.resources[0]
    assert row.size_bytes is None
    assert row.extra["not_measured"] == cs.GAUGE_REFUSED


def test_unreachable_buckets_are_a_gap_while_the_listed_rows_survive():
    """`Buckets.unreachable` lists buckets the API could not return, so it is a gap."""
    stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021")],
                                      "unreachable": ["projects/lab-prod-1/b/buckets/lost"]},
             "monitoring.projects.timeSeries.list": gauge()})
    found = cs.collect_storage(PROJECT)

    assert [r.name for r in found.resources] == ["lab-raw-2021"]
    assert found.gaps[0].read == "storage.buckets.list"
    assert "lost" in found.gaps[0].reason


def test_pages_are_followed_and_both_pages_are_rows():
    fake = stage({"storage.buckets.list": [{"items": [bucket("lab-raw-2021")],
                                              "nextPageToken": "CAISAggB"},
                                             {"items": [bucket("lab-backup-archive")]}],
                    "monitoring.projects.timeSeries.list": gauge()})
    found = cs.collect_storage(PROJECT)

    assert sorted(r.name for r in found.resources) == ["lab-backup-archive", "lab-raw-2021"]
    assert fake.count("storage.buckets.list") == 2


def test_an_empty_bucket_list_is_an_answer_not_a_gap():
    fake = stage({"storage.buckets.list": {},
                    "monitoring.projects.timeSeries.list": gauge()})
    found = cs.collect_storage(PROJECT)

    assert found.resources == [] and found.gaps == []
    assert fake.count("storage.buckets.list") == 1


# ------------------------------------------------- the one page that can settle a zero
#
# Every listing payload below is synthetic. The keys (`items`, `prefixes`, `nextPageToken`) follow
# `docs/API-EVIDENCE.md` §`objects.list`. Object entries carry `kind`, `name` and `size` as a real
# entry does; `size` is never summed.

def probe_rows(found: model.Collect) -> dict[str, model.Resource]:
    return {row.name: row for row in found.resources}


def test_a_gauge_silent_bucket_on_an_empty_finished_page_is_a_measured_zero():
    """Gauge silent, bucket not versioned, and one page with no entries and no next token: the size
    is 0. `size_source` names the object listing, so this zero is distinct from a gauge zero."""
    stage({"storage.buckets.list": {"items": [bucket("dataproc-temp-us-central1-000000000000-abcde")]},
           "monitoring.projects.timeSeries.list": gauge(),
           "storage.objects.list": {"items": []}})
    row = cs.collect_storage(PROJECT).resources[0]

    assert row.size_bytes == 0
    assert row.size_source == cs.READ_OBJECTS
    assert "not_measured" not in row.extra and "objects_probe" not in row.extra
    evidence = row.evidence()
    assert evidence["bytes"] == 0 and evidence["gib"] == 0.0
    assert evidence["size_source"] == "storage.objects.list"


def test_the_listing_is_one_page_of_one_object_and_is_asked_only_of_a_silent_bucket():
    """One call with `maxResults=1`, made only for the bucket the gauge did not size. One entry is
    enough to show a bucket is not empty."""
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-raw-2021"),
                                                     bucket("lab-cromwell-scratch")]},
                  "monitoring.projects.timeSeries.list": gauge(
                      series("lab-raw-2021",
                             [("2026-09-24T05:03:00Z", "2026-09-25T05:03:00Z", "1073741824")])),
                  "storage.objects.list": {"items": []}})
    rows = probe_rows(cs.collect_storage(PROJECT))

    assert fake.count(cs.READ_OBJECTS) == 1
    assert fake.urls(cs.READ_OBJECTS) == [
        "https://storage.googleapis.com/storage/v1/b/lab-cromwell-scratch/o"]
    assert fake.queries(cs.READ_OBJECTS) == [{"maxResults": cs.OBJECT_PROBE_PAGE}]
    assert cs.OBJECT_PROBE_PAGE == 1
    assert rows["lab-raw-2021"].size_bytes == 1073741824      # gauge-sized, never asked
    assert rows["lab-raw-2021"].size_source == cs.METRIC_TOTAL_BYTES
    assert rows["lab-cromwell-scratch"].size_bytes == 0


def test_an_empty_page_with_a_token_out_is_a_walk_that_did_not_finish_and_is_not_followed():
    """An empty page with a `nextPageToken` does not prove the bucket is empty. The token is not
    followed; the collector reads one page only."""
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-maybe-empty")]},
                  "monitoring.projects.timeSeries.list": gauge(),
                  "storage.objects.list": [{"items": [], "nextPageToken": "CAISAggB"},
                                           {"items": []}]})
    row = cs.collect_storage(PROJECT).resources[0]

    assert fake.count(cs.READ_OBJECTS) == 1                      # one page, no crawl
    assert row.size_bytes is None
    assert row.extra["objects_probe"] == cs.PROBE_UNFINISHED
    assert row.extra["not_measured"] == cs.NO_SERIES


def test_prefixes_without_items_are_not_an_empty_bucket():
    """A page with `prefixes` is not empty, even when `items` is."""
    stage({"storage.buckets.list": {"items": [bucket("lab-foldered")]},
           "monitoring.projects.timeSeries.list": gauge(),
           "storage.objects.list": {"items": [], "prefixes": ["cromwell-executions/"]}})
    row = cs.collect_storage(PROJECT).resources[0]

    assert row.size_bytes is None
    assert row.extra["objects_probe"] == cs.PROBE_UNFINISHED


def test_a_versioned_bucket_is_never_asked_and_its_silence_names_versioning():
    """`objects.list` without `versions` omits non-current versions, which are billed. A versioned
    bucket could list nothing and still hold data, so it is not asked and the row says why."""
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-versions",
                                                            versioning={"enabled": True})]},
                  "monitoring.projects.timeSeries.list": gauge(),
                  "storage.objects.list": {"items": []}})
    row = cs.collect_storage(PROJECT).resources[0]

    assert fake.count(cs.READ_OBJECTS) == 0
    assert row.size_bytes is None
    assert row.extra["objects_probe"] == cs.PROBE_VERSIONED
    assert row.extra["not_measured"] == cs.NO_SERIES
    _money_out, refusal = pricing.price(row, pricing.Rates())
    assert refusal.state == model.UNSIZED
    assert "versioning" in refusal.reason


def test_versioning_absent_or_off_is_the_state_the_listing_is_asked_about():
    """A missing `versioning` block and `enabled: false` both mean off, so the listing is asked.
    Only `enabled: true` stops it."""
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-no-block"),
                                                     bucket("lab-off",
                                                            versioning={"enabled": False})]},
                  "monitoring.projects.timeSeries.list": gauge(),
                  "storage.objects.list": {"items": []}})
    rows = probe_rows(cs.collect_storage(PROJECT))

    assert fake.count(cs.READ_OBJECTS) == 2
    assert [rows["lab-no-block"].size_bytes, rows["lab-off"].size_bytes] == [0, 0]


def test_a_refused_listing_is_a_gap_that_names_its_bucket_and_leaves_the_row_unobserved():
    """A 403 on the listing is not an empty bucket. The row stays unsized, and other buckets are
    unaffected."""
    fake = stage({"storage.buckets.list": {"items": [bucket("lab-private"),
                                                     bucket("lab-public")]},
                  "monitoring.projects.timeSeries.list": gauge(),
                  "storage.objects.list": {"items": []}},
                 failures={(cs.READ_OBJECTS, "lab-private"):
                           (403, "PERMISSION_DENIED storage.objects.list")})
    rows = probe_rows(cs.collect_storage(PROJECT))

    assert fake.count(cs.READ_OBJECTS) == 2
    assert rows["lab-private"].size_bytes is None
    assert rows["lab-private"].extra["objects_probe"] == cs.PROBE_REFUSED
    assert rows["lab-public"].size_bytes == 0


def test_the_refused_probe_writes_one_gap_per_bucket_that_could_not_be_asked():
    stage({"storage.buckets.list": {"items": [bucket("lab-private")]},
           "monitoring.projects.timeSeries.list": gauge()},
          failures={cs.READ_OBJECTS: (403, "PERMISSION_DENIED storage.objects.list")})
    found = cs.collect_storage(PROJECT)

    assert [(g.read, g.reason) for g in found.gaps] == [
        (cs.READ_OBJECTS, "lab-private: PERMISSION_DENIED storage.objects.list")]
    assert found.resources[0].size_bytes is None


def test_the_listing_never_makes_a_number_from_the_sizes_it_lists():
    """The listing never yields a byte total. Summing `items[].size` would need a full crawl, so a
    non-empty page means only "not empty"."""
    stage({"storage.buckets.list": {"items": [bucket("lab-huge")]},
           "monitoring.projects.timeSeries.list": gauge(),
           "storage.objects.list": {"items": [{"kind": "storage#object", "name": "a.cram",
                                               "size": "999999999999"},
                                              {"kind": "storage#object", "name": "b.cram",
                                               "size": "999999999999"}]}})
    row = cs.collect_storage(PROJECT).resources[0]

    assert row.size_bytes is None and row.size_source == ""
    assert "bytes" not in row.evidence()
    assert row.extra["objects_probe"] == cs.PROBE_OCCUPIED


def test_a_silence_sentence_names_the_window_it_asked_about():
    """The no-series reason names the window, so a reader knows how long the gauge was silent.
    It is built from `GAUGE_WINDOW`, so the two stay in step."""
    assert f"{cs.GAUGE_WINDOW.days} days" in cs.NO_SERIES
    stage({"storage.buckets.list": {"items": [bucket("lab-versions",
                                                    versioning={"enabled": True})]},
           "monitoring.projects.timeSeries.list": gauge()})
    row = cs.collect_storage(PROJECT).resources[0]
    cells = model.cells(model.Row.of(row, None, model.Unpriced(model.UNSIZED, "x")))

    assert f"not_measured=byte gauge asked about {cs.GAUGE_WINDOW.days} days emitted no series" \
        in cells[-1]


def test_every_probe_outcome_has_a_sentence_the_pricer_can_print():
    """The collector writes each listing outcome and `pricing` prints its sentence. The two sets
    must match, so a new outcome cannot go without a sentence."""
    assert {cs.PROBE_OCCUPIED, cs.PROBE_UNFINISHED, cs.PROBE_VERSIONED, cs.PROBE_REFUSED} \
        == set(pricing.OBJECT_PROBE_REASONS)
    for sentence in pricing.OBJECT_PROBE_REASONS.values():
        assert ";" not in sentence                      # `;` is the evidence separator
