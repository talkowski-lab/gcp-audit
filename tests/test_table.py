"""The writer: two files, a pinned header, and a README that says what the run did not know.

The header is compared byte for byte because spreadsheet formulas depend on the column names and
order (`docs/DESIGN.md` §The columns). The other tests check that each cell holds one fact and that
the README claims no more than the run established.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

import pytest
from fake_session import install

from gcp_audit import guard, model, table

COLUMNS = model.CSV_HEADER.split(",")
A, B = "lab-alpha-seq", "lab-beta-gen"
COMPUTE = "compute.googleapis.com"
MONEY = model.Money(Decimal("12.00"), "services/6F81-5844-456A/skus/PD-STD", "2026-09-01")
NOT_ENUMERATED = [
    "artifactregistry.googleapis.com", "cloudfunctions.googleapis.com",
    "container.googleapis.com", "dataproc.googleapis.com", "pubsub.googleapis.com",
    "run.googleapis.com", "sqladmin.googleapis.com",
]


def row(project: str = A, service: str = COMPUTE, kind: str = "Disk", item: str = "pd-one",
        *, money: model.Money | None = MONEY, state: str = "MEASURED", location: str = "us-c1-a",
        resource_state: str = "IN_USE", last: str | None = "2026-09-27",
        field: str | None = "lastAttachTimestamp",
        evidence: dict | None = None) -> model.Row:
    return model.Row(project=project, service=service, kind=kind, item=item, location=location,
                     state=resource_state, money=money, amount_state=state, last_activity=last,
                     activity_field=field,
                     evidence={"size_gb": 100} if evidence is None else evidence)


UNPRICED = model.Row(project=B, service="storage.googleapis.com", kind="Bucket", item="lab-raw",
                     location="EU", state="ACTIVE", money=None, amount_state="UNSIZED",
                     last_activity=None, activity_field=None,
                     evidence={"reason": "byte gauge emitted no series"})


def meta(**overrides) -> dict:
    base = {
        "run_utc": "2026-10-20T13:04:02Z",
        "scope": f"projects: {A}",
        "projects": [A, B],
        "layer1": guard.layer1_line(),
        "stats": {"requests": 21, "pages": 24, "retries": 1},
        "gaps": [model.Gap(project=A, service=COMPUTE, read="compute.snapshots.list",
                           reason="HTTP 403 PERMISSION_DENIED no access")],
        "services": {A: (["compute.googleapis.com", "container.googleapis.com"], None),
                     B: ([], "serviceusage.services.list: HTTP 403 PERMISSION_DENIED")},
        "not_enumerated": list(NOT_ENUMERATED),
        "reads": 14,
        "bq_storage": False,
    }
    base.update(overrides)
    return base


def written(tmp_path: Path, rows=None, **meta_kwargs) -> tuple[Path, Path, Path]:
    out = tmp_path / "run"
    table.write_bundle(out, rows if rows is not None else [row(), UNPRICED], meta=meta(**meta_kwargs))
    return out, out / table.RESOURCES_CSV, out / table.README_TXT


def cells_of(csv_path: Path) -> list[list[str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


@pytest.fixture(autouse=True)
def guarded():
    guard.reset()
    install(routes={})
    yield
    guard.reset()


# -------------------------------------------------------------------- resources.csv

def test_the_header_is_the_first_line_byte_for_byte_with_nothing_above_it(tmp_path):
    """A line above the header, or a renamed column, would break spreadsheet imports and
    formulas."""
    _out, csv_path, _readme = written(tmp_path)
    raw = csv_path.read_bytes()
    assert raw.split(b"\n", 1)[0] == model.CSV_HEADER.encode("utf-8")
    assert not raw.startswith(b"\n") and not raw.startswith(b"\xef\xbb\xbf")


def test_every_row_is_ordered_by_project_service_kind_and_item(tmp_path):
    rows = [row(item="z-last"), row(project=B, item="a-first"), row(kind="Instance", item="vm"),
            row(kind="Instance", item="aa")]
    _out, csv_path, _readme = written(tmp_path, rows)
    keys = [(r[0], r[1], r[2], r[3]) for r in cells_of(csv_path)[1:]]
    assert keys == [(A, COMPUTE, "Disk", "z-last"),
                    (A, COMPUTE, "Instance", "aa"),
                    (A, COMPUTE, "Instance", "vm"),
                    (B, COMPUTE, "Disk", "a-first")], (
        "sorting by name alone would interleave two projects' rows and make a filter by project "
        "a manual job")


def test_the_row_order_the_caller_handed_over_does_not_survive_into_the_file(tmp_path):
    """`write_bundle` sorts, so two runs over the same estate produce identical files that diff
    cleanly."""
    first_dir, first_csv, _ = written(tmp_path, [row(item="b"), row(item="a")])
    _second, second_csv, _readme = written(tmp_path / "again", [row(item="a"), row(item="b")])
    assert first_csv.read_bytes() == second_csv.read_bytes()


def test_a_name_with_a_comma_and_a_quote_survives_the_round_trip(tmp_path):
    """`csv.writer` quotes the name, so it stays in one cell."""
    awkward = 'weird,name;and "quote"'
    _out, csv_path, _readme = written(tmp_path, [row(item=awkward)])
    assert cells_of(csv_path)[1][3] == awkward
    assert len(cells_of(csv_path)[1]) == len(COLUMNS)


def test_a_row_the_pricer_declined_keeps_its_question_mark_and_its_reason(tmp_path):
    _out, csv_path, _readme = written(tmp_path)
    unpriced = [r for r in cells_of(csv_path)[1:] if r[0] == B][0]
    assert unpriced[COLUMNS.index("usd_per_month")] == "NA"
    assert unpriced[COLUMNS.index("price_tier")] == "NA"
    assert unpriced[COLUMNS.index("amount_state")] == "UNSIZED"
    assert "reason=byte gauge emitted no series" in unpriced[COLUMNS.index("evidence")]


def test_no_cell_holds_a_separator_that_packs_several_facts(tmp_path):
    _out, csv_path, _readme = written(tmp_path)
    for cells in cells_of(csv_path):
        for cell in cells:
            assert " · " not in cell and "\n" not in cell


def test_the_money_cell_is_a_number_and_the_currency_is_its_own_column(tmp_path):
    _out, csv_path, _readme = written(tmp_path)
    priced = [r for r in cells_of(csv_path)[1:] if r[0] == A][0]
    assert priced[COLUMNS.index("usd_per_month")] == "12.00"
    assert priced[COLUMNS.index("currency")] == "USD"


def test_a_header_row_and_one_line_per_row_and_nothing_else(tmp_path):
    """No totals line. A sum across mixed `amount_state` values and currencies cannot be checked
    and would be read as the estate's cost."""
    rows = [row(), row(item="two"), UNPRICED]
    _out, csv_path, _readme = written(tmp_path, rows)
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) + 1
    assert all(line.strip() for line in lines)


def test_the_lines_end_with_a_bare_newline_so_the_file_diffs_clean(tmp_path):
    _out, csv_path, _readme = written(tmp_path, [row()])
    raw = csv_path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")


def test_write_bundle_creates_the_directory_and_returns_exactly_two_paths(tmp_path):
    out = tmp_path / "deep" / "run"
    paths = table.write_bundle(out, [row()], meta=meta())
    assert [p.name for p in paths] == [table.RESOURCES_CSV, table.README_TXT]
    assert sorted(p.name for p in out.iterdir()) == ["README.txt", "resources.csv"]
    assert all(p.parent == out for p in paths)


def test_no_third_file_appears_beside_the_two(tmp_path):
    out, _csv, _readme = written(tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["README.txt", "resources.csv"]


def test_a_file_the_operator_put_there_is_still_there(tmp_path):
    """The writer replaces only its own two files and leaves other files in the directory."""
    out, _csv, _readme = written(tmp_path)
    (out / "notes.txt").write_text("mine\n", encoding="utf-8")
    table.write_bundle(out, [row()], meta=meta())
    assert (out / "notes.txt").read_text(encoding="utf-8") == "mine\n"


def test_an_empty_row_list_still_writes_a_header_and_no_rows(tmp_path):
    """`cli.py` refuses an empty scope first, but the writer still handles it cleanly."""
    _out, csv_path, _readme = written(tmp_path, [])
    assert cells_of(csv_path) == [COLUMNS]


def test_a_name_that_is_untrusted_data_stays_in_its_cell(tmp_path):
    """Names come from untrusted data. They go in a cell and are never used as a path, command
    or query."""
    hostile = "../../etc/passwd;rm -rf /$(whoami)"
    out, csv_path, readme = written(tmp_path, [row(item=hostile)])
    assert cells_of(csv_path)[1][3] == hostile
    assert sorted(p.name for p in out.iterdir()) == ["README.txt", "resources.csv"]
    assert hostile not in readme.read_text(encoding="utf-8")   # names belong in the CSV only


# ------------------------------------------------------------------------- README.txt

def test_the_readme_carries_the_layer1_line_byte_for_byte(tmp_path):
    line = guard.layer1_line()
    _out, _csv, readme = written(tmp_path)
    assert line in readme.read_text(encoding="utf-8")


def test_the_readme_states_the_run_time_the_scope_and_the_projects_it_covered(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    assert "2026-10-20T13:04:02Z" in text
    assert f"projects: {A}" in text
    assert A in text and B in text


def test_the_readme_carries_the_request_and_page_counts_the_run_recorded(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    assert "requests made: 21" in text
    assert "pages fetched: 24" in text
    assert "retries: 1" in text


def test_an_empty_stats_dict_is_an_answer_and_not_an_absence(tmp_path):
    """Zero requests is a recorded value. Only a missing key prints `not recorded`."""
    text = written(tmp_path, stats={"requests": 0, "pages": 0, "retries": 0})[2].read_text(
        encoding="utf-8")
    assert "requests made: 0" in text
    assert "requests made: not recorded" not in text


def test_glossary_keys_are_exactly_the_columns_of_the_header():
    """Every column has a glossary entry, and every entry names a current column."""
    assert sorted(table.GLOSSARY) == sorted(COLUMNS)


def test_the_money_column_glossary_states_the_monthly_convention_it_silently_contains():
    """A VM row is an hourly rate times 730. The glossary states that convention, using `model`'s
    own string so the text and the code cannot disagree."""
    from gcp_audit import pricing

    assert model.MONEY_CONVENTION in table.GLOSSARY["usd_per_month"]
    assert pricing.MONEY_CONVENTION is model.MONEY_CONVENTION
    assert pricing.HOURS_PER_MONTH == model.HOURS_PER_MONTH


def test_the_price_date_glossary_says_whose_date_it_is():
    """The catalog re-stamps `effectiveTime` on every record each day (`docs/API-EVIDENCE.md`
    §Cloud Billing Catalog). So `price_as_of` is the catalog's date, not the price's age, and the
    glossary must say so."""
    line = table.GLOSSARY["price_as_of"]
    assert "restamp" in line and "daily" in line
    assert "not the price's age" in line
    assert "the start date of the price" not in line


def test_every_column_is_glossed_in_the_readme_a_manager_opens(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    for column in COLUMNS:
        assert f"{column}: {table.GLOSSARY[column]}" in text, f"{column} is not glossed"


def test_every_gap_is_named_with_the_read_that_failed_and_what_it_said(tmp_path):
    gaps = [model.Gap(project=A, service=COMPUTE, read="compute.disks.aggregatedList",
                      reason="HTTP 403 PERMISSION_DENIED no access"),
            model.Gap(project=B, service="container.googleapis.com", read="not-read",
                      reason="enabled in this project and not enumerated by this version")]
    text = written(tmp_path, gaps=gaps)[2].read_text(encoding="utf-8")
    for gap in gaps:
        assert f"{gap.project}  {gap.service}  {gap.read}  {gap.reason}" in text


def test_a_run_with_no_gaps_says_the_collectors_reported_none_rather_than_that_all_is_well(
        tmp_path):
    text = written(tmp_path, gaps=[], services={A: ([], None)})[2].read_text(encoding="utf-8")
    assert "the collectors reported no read that failed" in text
    assert "HTTP 403" not in text


def test_the_services_this_build_never_reads_are_named(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    for name in NOT_ENUMERATED:
        assert name in text


def test_an_enabled_list_and_a_refused_service_read_are_different_sentences(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    assert f"{A}: compute.googleapis.com, container.googleapis.com" in text
    assert f"{B}: cannot state which APIs are enabled" in text
    assert "HTTP 403" in text


def test_a_project_whose_service_read_answered_and_found_nothing_says_that_instead(tmp_path):
    text = written(tmp_path, services={A: ([], None)})[2].read_text(encoding="utf-8")
    assert f"{A}: the read answered and reported no enabled service" in text


def test_an_absent_meta_key_prints_not_recorded_rather_than_a_plausible_guess(tmp_path):
    """`write_bundle` must not invent a run time, a layer-1 line or a service list when the
    caller did not record one."""
    out = tmp_path / "run"
    table.write_bundle(out, [row()], meta={})
    text = (out / table.README_TXT).read_text(encoding="utf-8")
    recorded = [line for line in text.splitlines() if table.NOT_RECORDED in line]
    assert "run (UTC): " + table.NOT_RECORDED in recorded
    assert "scope: " + table.NOT_RECORDED in recorded
    assert "requests made: " + table.NOT_RECORDED in recorded
    assert any(line.startswith("BigQuery reads:") for line in recorded)
    assert any(line.startswith("projects covered") for line in recorded)
    # The caller's two lists cover the rest.
    assert "reads this build is allowed to make: " + table.NOT_RECORDED in text
    assert "Reads that did not answer" in text


def test_the_layer1_line_is_the_live_guard_line_when_the_caller_did_not_record_one(tmp_path):
    """With no recorded line, the README uses the guard's own line from this process."""
    out = tmp_path / "run"
    table.write_bundle(out, [row()], meta={"layer1": "  "})
    assert guard.layer1_line() in (out / table.README_TXT).read_text(encoding="utf-8")


def test_the_readme_says_how_the_credential_line_was_established(tmp_path):
    """The README says how the layer-1 line was established, so a test-installed session is not
    reported as a token checked against tokeninfo."""
    text = written(tmp_path)[2].read_text(encoding="utf-8")
    assert "how that line was established: installed-by-test" in text
    assert "layer 1 state: ENFORCED" in text


def test_a_run_that_never_verified_a_credential_says_none_and_refused(tmp_path, monkeypatch):
    guard.reset()                     # nothing installed, so the state is REFUSED
    out = tmp_path / "run"
    table.write_bundle(out, [row()], meta=meta(layer1=None))
    text = (out / table.README_TXT).read_text(encoding="utf-8")
    assert "layer1 = REFUSED" in text
    assert "how that line was established: none (layer 1 state: REFUSED)" in text


def test_the_readme_states_what_layer1_does_not_check(tmp_path):
    """The README must say that roles and `testIamPermissions` are not checked
    (`docs/SAFETY.md` §What is not checked)."""
    text = written(tmp_path)[2].read_text(encoding="utf-8").lower()
    assert "testiampermissions" in text and "nothing" in text
    assert "role" in text


def test_the_readme_carries_no_total_no_sum_and_no_advice(tmp_path):
    rows = [row(money=model.Money(Decimal("12.00"), "s/a", "2026-09-01")),
            row(item="two", money=model.Money(Decimal("13.33"), "s/b", "2026-09-01")),
            UNPRICED]
    _out, csv_path, readme_path = written(tmp_path, rows)
    text = readme_path.read_text(encoding="utf-8")
    lowered = text.lower()
    for banned in ("total", "sum(", "sum :", " recommend", "you should", "delete ",
                   "remove ", "cheapest", "waste"):
        assert banned not in lowered, f"README.txt says {banned!r}"
    summed = sum(Decimal(c[COLUMNS.index("usd_per_month")])
                 for c in cells_of(csv_path)[1:]
                 if c[COLUMNS.index("usd_per_month")] != model.UNKNOWN)
    assert f"{summed:.2f}" not in text      # a reader may compute a sum; the README does not


def test_the_readme_does_not_restate_a_row(tmp_path):
    """Row facts live only in the table, so the README repeats no resource name."""
    _out, _csv, readme = written(tmp_path, [row(item="wdl-worker-7"), row(item="pd-forgotten-3")])
    text = readme.read_text(encoding="utf-8")
    assert "wdl-worker-7" not in text and "pd-forgotten-3" not in text


def test_the_readme_says_a_catalog_number_is_not_an_invoice(tmp_path):
    text = written(tmp_path)[2].read_text(encoding="utf-8").lower()
    assert "not an invoice" in text


#: The line whole, in both flag states.
BQ_READS_AUTHORISED = (
    "BigQuery reads: the region query (INFORMATION_SCHEMA.TABLE_STORAGE, one per dataset location, "
    "charged to the project's own slot time): authorised; the per-table metadata read "
    "(bigquery.tables.get, no slot time) sizes every table the region query does not, up to the "
    "request budget left to this run")
BQ_READS_NOT_AUTHORISED = BQ_READS_AUTHORISED.replace("): authorised;",
                                                      "): not authorised for this run;")


def test_the_readme_names_both_bigquery_reads_and_which_one_was_authorised(tmp_path):
    """One line names both BigQuery reads.

    `bigquery.tables.get` runs with or without `--read-bq-storage`, so its half of the line is the
    same in both states. Only the region query's half changes.
    """
    yes = written(tmp_path, bq_storage=True)[2].read_text(encoding="utf-8")
    no = written(tmp_path, bq_storage=False)[2].read_text(encoding="utf-8")
    assert "authorised" in yes and "not authorised" not in yes
    assert "not authorised for this run" in no

    def header(text: str) -> list[str]:
        return [line for line in text.splitlines() if line.startswith("BigQuery reads:")]

    assert len(header(yes)) == 1 and len(header(no)) == 1, (header(yes), header(no))
    assert header(yes)[0] == BQ_READS_AUTHORISED, header(yes)[0]
    assert header(no)[0] == BQ_READS_NOT_AUTHORISED, header(no)[0]
    assert "the region query" in header(yes)[0]
    assert "bigquery.tables.get" in header(yes)[0]
    # The metadata half is identical in both. Split on the phrase that introduces it.
    split = "; the per-table metadata read"
    assert split in header(yes)[0] and split in header(no)[0]
    assert header(yes)[0].split(split)[1] == header(no)[0].split(split)[1]
    assert header(yes)[0].count("authorised") == 1        # the query half, and only that half
    assert "not authorised" in header(no)[0]
    # The two lines differ only in the state clause.
    assert header(no)[0] == header(yes)[0].replace(": authorised;",
                                                  ": not authorised for this run;", 1)


def test_the_readme_is_the_only_prose_file(tmp_path):
    """README.txt is the only prose file in the output."""
    out, _csv, _readme = written(tmp_path)
    prose = [p for p in out.iterdir() if p.suffix in {".md", ".html", ".txt"}]
    assert [p.name for p in prose] == ["README.txt"]


# ------------------------------------------------------------------ cells come from model

def test_the_writer_writes_no_cells_of_its_own(tmp_path):
    """Every cell comes from `model.cells`, so formatting rules live in one place."""
    rows = [row(), UNPRICED]
    _out, csv_path, _readme = written(tmp_path, rows)
    expected = [model.cells(r) for r in sorted(rows, key=lambda r: (r.project, r.service,
                                                                    r.kind, r.item))]
    assert cells_of(csv_path)[1:] == expected


def test_a_row_written_through_a_stringio_writer_reads_back_identically(tmp_path):
    """The file uses `csv`'s own quoting, so `csv.reader` reads back the cells written."""
    rows = [row(item='a,b "c"')]
    _out, csv_path, _readme = written(tmp_path, rows)
    buffer = io.StringIO(csv_path.read_text(encoding="utf-8"))
    assert list(csv.reader(buffer)) == cells_of(csv_path)


# ------------------------------------------------ the TERMINATED-instance line, and its four states

#: A typical record: 30 VM rows collected, 29 stopped, the default in force, and the instance read
#: complete. The line needs all four keys.
WITHHELD = {"collected": 30, "terminated": 29, "listed": False, "read_incomplete": False}


def terminated_text(tmp_path, record) -> str:
    text = written(tmp_path, terminated_instances=record)[2].read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("Instance rows reporting TERMINATED:")]
    assert len(lines) == 1, lines
    return lines[0]


def test_the_readme_counts_the_vm_rows_it_withheld_and_names_the_flag_that_returns_them(tmp_path):
    """The line gives the count and the flag that adds the rows back.

    It also says the VM's disks keep their own rows, so leaving the VM out does not read as "costs
    nothing" (`docs/API-EVIDENCE.md` §`Instance.status`).
    """
    line = terminated_text(tmp_path, WITHHELD)
    assert "29 of 30 left out of resources.csv by default" in line
    assert table.INCLUDE_TERMINATED_FLAG in line
    assert "Its persistent disks are billed whatever its state and keep their own rows" in line


def test_the_readme_says_the_flag_was_given_instead_of_claiming_a_withholding(tmp_path):
    line = terminated_text(tmp_path, {**WITHHELD, "listed": True})
    assert f"29 of 30 listed, because {table.INCLUDE_TERMINATED_FLAG} was given" in line
    assert "left out" not in line


def test_a_zero_is_an_answer_unless_the_read_that_would_have_proved_it_did_not_answer(tmp_path):
    """`0 of 30` is an observation. `0 of 0` after an incomplete instance read is not, and the
    line says so.
    """
    quiet = terminated_text(tmp_path, {**WITHHELD, "terminated": 0})
    assert "0 of 30" in quiet and "nothing was left out" in quiet
    assert "did not answer" not in quiet

    partial = terminated_text(tmp_path, {**WITHHELD, "terminated": 0, "collected": 0,
                                         "read_incomplete": True})
    assert "0 of 0" in partial
    assert "The instances read did not answer for all of this scope" in partial


def test_a_partial_record_prints_not_recorded_rather_than_the_writer_choosing_a_branch(tmp_path):
    """Without all four keys the line prints `not recorded` rather than guessing a branch."""
    for record in ({"collected": 30, "terminated": 0, "read_incomplete": False},
                   {"terminated": 3, "listed": False, "read_incomplete": False},
                   "no"):
        line = terminated_text(tmp_path, record)
        assert line.endswith(table.NOT_RECORDED), line
