"""table.py — the artifact: `resources.csv` and the one page of prose that explains it.

Data goes in the CSV, one fact per column; prose goes in README.txt and nowhere else
(`docs/DESIGN.md` §The columns).

* The header is `model.CSV_HEADER`, byte for byte, with nothing above it: formulas use its names.
* A cell is written by `model.cells` through `csv.writer`, so a name with a comma stays one cell.
* Rows sort by project, service, kind, item, so two runs diff byte for byte.
* README.txt states no totals: a sum across mixed `amount_state` would quietly skip every `NA`.
"""
from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from gcp_audit import guard, model

RESOURCES_CSV = "resources.csv"
README_TXT = "README.txt"

#: What an unrecorded `meta` fact says in README.txt, never a plausible default.
NOT_RECORDED = "not recorded"

#: One entry per column of `model.CSV_HEADER`; `tests/test_table.py` pins the keys to the header.
GLOSSARY: dict[str, str] = {
    "project": "the project id the resource lives in; never `NA`",
    "service": "the API domain the row was read from, e.g. compute.googleapis.com; never `NA`",
    "kind": "Instance, Disk, Image, Snapshot, Address, Bucket or Table; never `NA`",
    "item": "the resource name as that API gave it; `NA` when it gave none",
    "location": "zone, region or global; `NA` when the read did not say",
    "state": "the state that API reports, e.g. RUNNING or ACTIVE; `NA` when it has no state field",
    "usd_per_month": "a number and nothing else: measured quantity x one dated list price per "
                     "month; " + model.MONEY_CONVENTION + "; `NA` when there is no honest number",
    "price_tier": "CATALOG when a measured quantity met a dated list price; `NA` when no price "
                  "was used",
    "price_as_of": "the price record's own date, which the catalog restamps daily: the sheet's date, "
                   "not the price's age; `NA` when no price was used",
    "currency": "the currency that rate was quoted in, which is a column rather than a suffix "
                "because `50.00 USD` in one cell makes a spreadsheet read the money as nothing; "
                "`NA` when no price was used",
    "amount_state": "MEASURED, UNSIZED (no size was read), UNPRICED (a size and no honest rate) "
                    "or REFUSED (the read that would have settled it did not answer); never `NA`",
    "last_activity": "YYYY-MM-DD, the last date anything reported this touched; `NA` when nothing "
                     "observed a date",
    "last_activity_field": "which field answered, e.g. lastStartTimestamp; says `not-read` when "
                           "none did, and is never `NA`",
    "evidence": "key=value pairs, keys sorted, one key per observed fact, carrying the reason "
                "behind an amount_state that is not MEASURED; says `no-evidence-recorded` when "
                "nobody observed anything",
}

MONEY_NOTE = (
    "Money here is a measured quantity times a dated official list price, one price per line. "
    "That is a run-rate estimate at one date, not an invoice: no committed-use or sustained-use "
    "discount is in it, because this pass never saw a bill."
)
DATE_NOTE = (
    "A date is only as good as the field that gave it, which is why last_activity_field is a "
    "column. lastStartTimestamp is a boot, not a use; updated on a bucket is a metadata change, "
    "not a read of the data; nothing in this version reports last-access for an object."
)
NOT_CHECKED_NOTE = (
    "What the line above does not claim: nothing in this pass reads an IAM policy, nothing calls "
    "*.testIamPermissions, and nothing established that this credential could not write. The run "
    "is read-only by construction, not by having verified the role set."
)


def _text(value: object) -> str:
    """A recorded value, or `NOT_RECORDED`. An empty string is not recorded; `0` or `False` is an
    answer and prints as one."""
    if value is None:
        return NOT_RECORDED
    if isinstance(value, str):
        return value if value.strip() else NOT_RECORDED
    return str(value)


def _yes_no(value: object, *, yes: str, no: str) -> str:
    if value is None:
        return NOT_RECORDED
    return yes if value else no


def _projects_line(meta: dict) -> str:
    projects = meta.get("projects")
    if projects is None:
        return f"projects covered: {NOT_RECORDED}"
    if isinstance(projects, str):
        return f"projects covered: {projects or NOT_RECORDED}"
    listed = ", ".join(sorted(str(project) for project in projects))
    return f"projects covered: {listed or 'none: the scope resolved to no project'}"


def _stats_lines(meta: dict) -> list[str]:
    """What the run counted about itself, off `guard.stats()`; not a statement about the estate."""
    stats = meta.get("stats")
    if not isinstance(stats, dict):
        return [f"requests made: {NOT_RECORDED}"]
    return [f"{label}: {stats[key] if key in stats else NOT_RECORDED}"
            for key, label in (("requests", "requests made"), ("pages", "pages fetched"),
                               ("retries", "retries"))]


def _gap_lines(gaps: object) -> list[str]:
    """One line per `model.Gap`: project, service, the method id that failed, what it said."""
    if gaps is None:
        return [NOT_RECORDED]
    lines = [f"{gap.project}  {gap.service}  {gap.read}  {gap.reason}" for gap in gaps]
    return lines or ["none: the collectors reported no read that failed"]


def _service_lines(services: object) -> list[str]:
    """Per project, what `serviceusage.services.list` answered. A failed read is never printed as
    an empty list."""
    if not isinstance(services, dict):
        return [NOT_RECORDED]
    if not services:
        return ["no project's service list was recorded by the caller"]
    lines: list[str] = []
    for project in sorted(services):
        names, reason = services[project]
        if reason:
            lines.append(f"{project}: cannot state which APIs are enabled — {reason}")
        elif names:
            lines.append(f"{project}: {', '.join(sorted(names))}")
        else:
            lines.append(f"{project}: the read answered and reported no enabled service")
    return lines


def _enumerated_lines(names: object) -> list[str]:
    """The APIs this build reads nothing from, named — from the caller's list, never a guess."""
    if names is None:
        return [NOT_RECORDED]
    if isinstance(names, str):        # one name: iterating a str would yield letters
        return [names]
    listed = sorted(str(name) for name in names)
    return listed or ["none named by the caller, so this table makes no claim about them"]


#: The two BigQuery reads; `--read-bq-storage` chooses which one sizes the tables.
BQ_QUERY_HALF = ("the region query (INFORMATION_SCHEMA.TABLE_STORAGE, one per dataset location, "
                 "charged to the project's own slot time): ")
BQ_METADATA_HALF = ("the per-table metadata read (bigquery.tables.get, no slot time) sizes every "
                    "table the region query does not, up to the request budget left to this run")


def _bq_reads_line(meta: dict) -> str:
    """Both BigQuery reads on one line: which one was authorised, and what the other did."""
    return ("BigQuery reads: " + BQ_QUERY_HALF
            + _yes_no(meta.get("bq_storage"), yes="authorised", no="not authorised for this run")
            + "; " + BQ_METADATA_HALF)


#: The flag that lists TERMINATED VMs, named in the README line that mentions the omission.
INCLUDE_TERMINATED_FLAG = "--include-terminated"


def _terminated_line(meta: dict) -> str:
    """The TERMINATED VM rows, counted over what the instances read named.

    `read_incomplete` separates an observed zero from a read that did not answer; a partial
    record prints `not recorded`. The billing claim is quoted in `docs/API-EVIDENCE.md`
    §`Instance.status`.
    """
    record = meta.get("terminated_instances")
    if not isinstance(record, dict) or any(key not in record for key in
                                           ("collected", "terminated", "listed",
                                            "read_incomplete")):
        return f"Instance rows reporting TERMINATED: {NOT_RECORDED}"
    stopped, collected = _text(record["terminated"]), _text(record["collected"])
    if record["listed"]:
        return (f"Instance rows reporting TERMINATED: {stopped} of {collected} listed, because "
                f"{INCLUDE_TERMINATED_FLAG} was given. A VM in that state bills no vCPU and no "
                "memory, so money on such a row is what the machine costs switched on, not what it "
                "costs today.")
    if record["terminated"]:
        return (f"Instance rows reporting TERMINATED: {stopped} of {collected} left out of "
                f"resources.csv by default — a stopped VM bills no vCPU and no memory, so its money "
                "cell is a machine nobody has switched on. Its persistent disks are billed whatever "
                f"its state and keep their own rows. {INCLUDE_TERMINATED_FLAG} lists them.")
    return (f"Instance rows reporting TERMINATED: {stopped} of {collected} — none reported that "
            "state, so nothing was left out."
            + (" The instances read did not answer for all of this scope, so that count is over what "
               "it did answer, not over the scope." if record["read_incomplete"] else ""))


def _layer1_lines(meta: dict) -> list[str]:
    """The guard's own sentence, then how it was established: a test-installed session is also
    ENFORCED but was never introspected, so the provenance is printed with it."""
    line = meta.get("layer1")
    line = line if isinstance(line, str) and line.strip() else guard.layer1_line()
    status = guard.layer1_status()
    return [line,
            f"how that line was established: {_text(status.get('verified_via'))} "
            f"(layer 1 state: {_text(status.get('state'))}); a session installed by a test is "
            "named as one and is not evidence about anyone's credential",
            ""]


def readme_text(meta: dict) -> str:
    """README.txt as one string, in the order a reader needs it: when, what, whether the run was
    allowed, what did not answer, what was never looked at, and what the columns mean."""
    lines: list[str] = [
        "gcp-audit — resources.csv, and what this pass did not establish",
        "=" * 72,
        "",
        f"run (UTC): {_text(meta.get('run_utc'))}",
        f"scope: {_text(meta.get('scope'))}",
        _projects_line(meta),
        _bq_reads_line(meta),
        # Declared, not silently absent: datasets are containers; their tables are the rows.
        "BigQuery datasets are read for placement and are not listed as rows: a dataset has no "
        "billing unit (its tables do), the dataset list answers no date for it, and each table row "
        "already names the dataset it lives in.",
        _terminated_line(meta),
        f"reads this build is allowed to make: {_text(meta.get('reads'))}",
        *_stats_lines(meta),
        "",
    ]
    # Without the caller's `layer1` record, the live guard in this process states itself.
    lines += [*_layer1_lines(meta),
              NOT_CHECKED_NOTE,
              "",
              "Services this build reads nothing from, whether or not a project has them on",
              "-" * 60,
              *_enumerated_lines(meta.get("not_enumerated")),
              "",
              "Which APIs each project has enabled, as far as one read could tell",
              "-" * 60,
              *_service_lines(meta.get("services")),
              "",
              "Reads that did not answer, and what they said",
              "-" * 60,
              *_gap_lines(meta.get("gaps")),
              "",
              "The columns of resources.csv",
              "-" * 60,
              *(f"{column}: {GLOSSARY[column]}" for column in model.CSV_HEADER.split(",")),
              "",
              MONEY_NOTE,
              "",
              DATE_NOTE,
              ""]
    return "\n".join(lines)


def write_bundle(outdir: Path, rows: Sequence[model.Row], *, meta: dict) -> list[Path]:
    """Write `resources.csv` and `README.txt` into `outdir` (created if absent); return both paths.
    A `meta` key the caller did not supply prints `not recorded`."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    csv_path = outdir / RESOURCES_CSV
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        handle.write(model.CSV_HEADER + "\n")
        writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        for row in sorted(rows, key=lambda r: (r.project, r.service, r.kind, r.item)):
            writer.writerow(model.cells(row))

    readme_path = outdir / README_TXT
    readme_path.write_text(readme_text(dict(meta)), encoding="utf-8")
    return [csv_path, readme_path]
