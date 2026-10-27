"""The row, its cells, and the rules for when a cell says `NA`."""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from gcp_audit import model

PROJECT = "lab-seq-prd"
COMPUTE = "compute.googleapis.com"


def resource(**kwargs) -> model.Resource:
    base = dict(project=PROJECT, service=COMPUTE, kind="Disk", name="pd-worker-1")
    base.update(kwargs)
    return model.Resource(**base)


MONEY = model.Money(Decimal("12.00"), "services/6F81-5844-456A/skus/DISK- STANDARD", "2026-09-01")


# ------------------------------------------------------------------ the header

def test_the_header_is_pinned_byte_for_byte():
    """Spreadsheet formulas refer to these names, so a rename is a breaking change."""
    assert model.CSV_HEADER == (
        "project,service,kind,item,location,state,usd_per_month,price_tier,price_as_of,"
        "currency,amount_state,last_activity,last_activity_field,evidence")


def test_every_row_writes_exactly_one_cell_per_column():
    row = model.Row.of(resource(size_gb_api=100, last_activity="2026-01-02",
                                activity_field="lastAttachTimestamp"), MONEY, None)
    assert len(model.cells(row)) == len(model.CSV_HEADER.split(","))


def test_no_column_name_is_a_sentence_and_none_is_uppercase():
    for name in model.CSV_HEADER.split(","):
        assert name.replace("_", "").islower()
        assert " " not in name and len(name) <= 20


def test_no_cell_holds_a_separator_that_packs_several_facts():
    """No cell packs several facts behind a ` · ` separator or a newline."""
    row = model.Row.of(resource(size_gb_api=100, cpus=8, memory_mb=32768, state="RUNNING",
                                location="us-central1-a", last_activity="2026-02-03",
                                activity_field="lastStartTimestamp",
                                extra={"machine_type": "e2-standard-8"}), MONEY, None)
    for cell in model.cells(row):
        assert " · " not in cell and "\n" not in cell


# ------------------------------------------------------------------ money honesty

def test_a_priced_row_names_the_catalog_record_the_number_came_from():
    """The price comes from the catalog, not the resource's API. The evidence names the SKU so a
    reader can check it; `price_as_of` alone does not identify the record."""
    row = model.Row.of(resource(size_gb_api=100), MONEY, None)
    assert row.evidence["sku"] == "services/6F81-5844-456A/skus/DISK- STANDARD"


def test_a_sized_row_names_the_read_that_produced_the_size():
    """A size can come from a gauge, a BigQuery view or the resource's own record, which differ in
    reliability. The evidence names the source."""
    row = model.Row.of(resource(kind="Bucket", size_bytes=2 * model.GIB, size_source="gauge"),
                       None, model.Unpriced(model.UNPRICED, "no dated standard storage rate for EU"))
    assert row.evidence["size_source"] == "gauge"
    row2 = model.Row.of(resource(size_bytes=model.GIB), None,
                        model.Unpriced(model.UNSIZED, "disk sizeGb unobserved"))
    assert "size_source" not in row2.evidence        # unobserved is absent, not an empty string


def test_the_money_cell_is_numeric_and_the_currency_is_not_in_it():
    """A cell like `50.00 USD` breaks SUM(), so the currency has its own column."""
    row = model.Row.of(resource(size_gb_api=100), MONEY, None)
    cells = model.cells(row)
    amount = cells[model.CSV_HEADER.split(",").index("usd_per_month")]
    assert amount == "12.00"
    float(amount.replace(".", ""))                       # parses as a number, not a phrase
    assert cells[model.CSV_HEADER.split(",").index("currency")] == "USD"


def test_a_non_usd_rate_says_so_in_the_currency_column_rather_than_letting_the_header_lie():
    euro = model.Money(Decimal("9.00"), "services/x/skus/EU", "2026-09-01", currency="EUR")
    cells = model.cells(model.Row.of(resource(size_gb_api=100), euro, None))
    assert cells[9] == "EUR"


def test_a_row_that_could_not_be_priced_prints_a_question_mark_and_never_zero():
    for state, reason in [("UNSIZED", "byte gauge emitted no series"),
                          ("UNPRICED", "no dated e2 rate for europe-west1"),
                          ("REFUSED", "HTTP 403 on the price sheet")]:
        cells = model.cells(model.Row.of(resource(), None,
                                         model.Unpriced(state, reason)))
        assert cells[6] == "NA"
        assert cells[10] == state
        assert f"reason={reason}" in cells[13]


def test_measured_requires_money_and_every_other_state_forbids_it():
    with pytest.raises(ValueError):
        model.Row(project=PROJECT, service=COMPUTE, kind="Disk", item="d", location="us",
                  state="", money=None, amount_state="MEASURED", last_activity=None,
                  activity_field=None)
    with pytest.raises(ValueError):
        model.Row(project=PROJECT, service=COMPUTE, kind="Disk", item="d", location="us",
                  state="", money=MONEY, amount_state="UNPRICED", last_activity=None,
                  activity_field=None)
    with pytest.raises(ValueError):
        model.Row(project=PROJECT, service=COMPUTE, kind="Disk", item="d", location="us",
                  state="", money=MONEY, amount_state="almost", last_activity=None,
                  activity_field=None)


def test_a_measured_row_needs_no_refusal_and_carries_no_reason_key():
    cells = model.cells(model.Row.of(resource(size_gb_api=100), MONEY, None))
    assert "reason=" not in cells[13]


# ------------------------------------------------------------------ unobserved is not zero

def test_an_unobserved_size_is_absent_from_evidence_not_zero():
    """`size_gb=0` means a measured zero. A disk with no observed size has no key."""
    observed = model.cells(model.Row.of(resource(size_gb_api=0), MONEY, None))[13]
    unobserved = model.cells(model.Row.of(resource(), None,
                                          model.Unpriced("UNSIZED", "no size read")))[13]
    assert "size_gb=0" in observed          # a measured zero is an answer and is written
    assert "size_gb" not in unobserved
    assert "size_gb=" not in unobserved.split("reason=")[0]


def test_a_row_that_observed_nothing_says_so_in_words():
    cells = model.cells(model.Row.of(resource(), None, model.Unpriced("UNPRICED", "no rate")))
    assert "no-evidence-recorded" not in cells[13]      # `reason` is itself an observed fact
    row = model.Row(project=PROJECT, service=COMPUTE, kind="Disk", item="d", location="NA",
                    state="NA", money=None, amount_state="UNPRICED", last_activity=None,
                    activity_field=None)
    assert "no-evidence-recorded" in model.cells(row)[13]


def test_an_unread_date_is_a_question_mark_and_a_missing_field_says_not_read():
    cells = model.cells(model.Row.of(resource(), None, model.Unpriced("UNPRICED", "no rate")))
    assert cells[11] == "NA"
    assert cells[12] == "not-read"


@pytest.mark.parametrize("value,expected", [
    ("2026-09-30T12:00:00Z", "2026-09-30"),
    ("2026-09-30T12:00:00.123456Z", "2026-09-30"),
    ("2026-09-30", "2026-09-30"),
    ("2026-09-30T12:00:00+00:00", "2026-09-30"),
    ("1759000000", "2025-09-27"),
    ("1759000000000", "2025-09-27"),          # BigQuery's milliseconds
    (1759000000000, "2025-09-27"),
    ("1.759E9", "2025-09-27"),                 # BigQuery REST TIMESTAMP cell
    ("1759000000.123456", "2025-09-27"),
    (None, None),
    ("", None),
    ("0", None),                               # epoch zero is a bug, not a date
    ("-1", None),
    ("nonsense", None),
    ("99999999999999", None),                  # after 2100: not a date anyone means
])
def test_a_timestamp_that_is_not_a_date_never_becomes_1970(value, expected):
    assert model.date_only(value) == expected


# ------------------------------------------------------------------ units and the writer

def test_gib_is_the_only_conversion_and_there_is_no_decimal_gb_constant():
    assert model.GIB == 1024 ** 3
    source = open("src/gcp_audit/model.py", encoding="utf-8").read()
    assert "1000000000" not in source.replace("_", "")


def test_the_writer_quotes_a_name_with_a_comma_instead_of_splitting_the_row():
    row = model.Row.of(resource(name="weird,name;and \"quote\"", size_gb_api=1), MONEY, None)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(model.CSV_HEADER.split(","))
    writer.writerow(model.cells(row))
    written = list(csv.reader(io.StringIO(buffer.getvalue())))
    assert len(written[1]) == len(model.CSV_HEADER.split(","))
    assert written[1][3] == "weird,name;and \"quote\""


def test_evidence_keys_are_sorted_because_two_runs_must_write_the_same_bytes():
    row = model.Row.of(resource(size_bytes=2 * model.GIB, cpus=2, memory_mb=8192,
                                extra={"machine_type": "n2-standard-2",
                                       "attached_to": "inst-1"}), MONEY, None)
    evidence = model.cells(row)[13]
    keys = [pair.split("=")[0] for pair in evidence.split(";")]
    assert keys == sorted(keys)


def test_a_gap_is_stated_with_the_read_that_caused_it():
    collect = model.Collect(service=COMPUTE)
    collect.refused("compute.snapshots.list", "HTTP 403 PERMISSION_DENIED", PROJECT)
    assert collect.resources == [] and len(collect.gaps) == 1
    gap = collect.gaps[0]
    assert (gap.project, gap.read, gap.service) == (PROJECT, "compute.snapshots.list", COMPUTE)
    assert collect.resources == []      # an empty list stays "nothing found", not "could not tell"


def test_a_snapshot_s_storage_places_print_because_they_decide_the_price():
    """`storageLocations` is an array, and a snapshot in two places cannot be priced. The evidence
    lists the places so the refusal can be checked."""
    row = model.Row.of(model.Resource(
        kind="Snapshot", name="snap-1", project=PROJECT, service=COMPUTE, location="global",
        size_bytes=10 * model.GIB, size_source="compute.snapshots.list",
        extra={"storage_locations": "us-central1,eu-west1"}), None, None)
    assert "storage_locations=us-central1,eu-west1" in model.cells(row)[13]


def test_an_observed_empty_answer_cannot_hide_behind_the_truthiness_filter():
    """`evidence()` skips falsy `extra` values, which would hide an API answer of an empty list.
    `storage_locations_status` records that case so it differs from no answer."""
    silent = model.Row.of(model.Resource(kind="Snapshot", name="s", project=PROJECT,
                                         service=COMPUTE, size_bytes=model.GIB), None, None)
    empty = model.Row.of(model.Resource(kind="Snapshot", name="s", project=PROJECT,
                                        service=COMPUTE, size_bytes=model.GIB,
                                        extra={"storage_locations_status": "answered-empty"}),
                         None, None)
    assert "storage_locations" not in model.cells(silent)[13]
    assert "storage_locations_status=answered-empty" in model.cells(empty)[13]
    assert model.cells(empty)[13] != model.cells(silent)[13]
