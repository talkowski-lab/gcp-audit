"""collect_bigquery.py — BigQuery tables, sized by one of two reads.

`tables.list` carries no size, so every table row is sized by exactly one of:

* `--read-bq-storage`: one `INFORMATION_SCHEMA.TABLE_STORAGE` query per dataset location. It is
  charged to the project's slot time, so it is asked for, never implied. It answers the size, the
  active/long-term split and `storage_last_modified_time` (the last data write) for every table in
  that location. Google publishes no last-read time for a table, here or anywhere else.
* otherwise: one `bigquery.tables.get` per table. A metadata read, no slot time, bounded by the
  request budget the process has left and by `MAX_GET_REFUSALS`.

A dataset whose `location` is not a well-formed location id is never interpolated into SQL; its
tables are sized by `tables.get` in either mode. Datasets are read for their location and are not
rows: Google bills the tables.
"""
from __future__ import annotations

import re
from typing import Any

from gcp_audit import guard, methods, model

SERVICE = "bigquery.googleapis.com"
READ_DATASETS = "bigquery.datasets.list"
READ_TABLES = "bigquery.tables.list"
READ_TABLE = "bigquery.tables.get"
READ_QUERY = "bigquery.jobs.query"

#: The one query. Columns are the documented `TABLE_STORAGE` schema; the view must be qualified as
#: `region-<location>`, which accepts a single region (`us-central1`) or a multi-region (`us`).
TABLE_STORAGE_SQL = ("SELECT table_schema, table_name, total_logical_bytes, active_logical_bytes, "
                     "long_term_logical_bytes, storage_last_modified_time "
                     "FROM `{project}`.`region-{region}`.INFORMATION_SCHEMA.TABLE_STORAGE "
                     "WHERE deleted IS NOT TRUE")
TABLE_STORAGE_COLUMNS = ("table_schema", "table_name", "total_logical_bytes",
                         "active_logical_bytes", "long_term_logical_bytes",
                         "storage_last_modified_time")
VIEW_DATE_COLUMN = "storage_last_modified_time"

#: Cost cap on each query (`QueryRequest.maximumBytesBilled`, an int64 sent as a string): "Queries
#: with bytes billed above this limit will fail (without incurring a charge)." An INFORMATION_SCHEMA
#: query bills a 10 MB minimum on demand, so 100 MiB leaves room and still bounds a surprise. A
#: query that hits it fails, and the failure is a gap like any other refused query.
MAX_BYTES_BILLED = 100 * 1024 * 1024

#: A location id is never free text: no dot, space, quote or backtick passes, which is what makes
#: interpolating it safe.
REGION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,29}$")

#: What `extra["not_measured"]` says. None of them is a zero.
NOT_READ = "TABLE_STORAGE not read (--read-bq-storage)"
LOCATION_UNREADABLE = "TABLE_STORAGE skipped: dataset location unreadable"
READ_REFUSED = "TABLE_STORAGE read did not answer"
NO_BYTES = "TABLE_STORAGE gave no byte count"
TRUNCATED = "TABLE_STORAGE result was truncated before this table"
NAME_AMBIGUOUS = "TABLE_STORAGE reported this table more than once"
NO_BYTES_GET = "tables.get gave no byte count"
NOT_ASKED_BUDGET = "tables.get never asked (request budget spent)"
NO_TABLE_ID = "tables.list gave no tableId, so no read can address this table"

#: The rows `tables.get` is sent for. A refused, truncated or ambiguous view is deliberately not in
#: this set: falling back per table there would turn one denied query into a GET per table.
SIZED_BY_GET = frozenset({NOT_READ, LOCATION_UNREADABLE})

#: A denial is per credential: three refusals in a row stop the per-table reads for the project.
MAX_GET_REFUSALS = 3

#: `tables.get` field names for the 90-day age split the catalog bills on.
SPLIT_ACTIVE_FIELD = "numActiveLogicalBytes"
SPLIT_LONG_TERM_FIELD = "numLongTermLogicalBytes"
SPLIT_TOTAL_FIELD = "numTotalLogicalBytes"


def table_storage_sql(project: str, region: str) -> str:
    """The query for one project and one location, or a refusal before any text is built."""
    if not model.PROJECT_ID_RE.match(project):
        raise guard.AuditError(f"{project!r} is not a project id, so no query is built")
    if not REGION_RE.match(region):
        raise guard.AuditError(f"{region!r} is not a location id, so no query is built")
    return TABLE_STORAGE_SQL.format(project=project, region=region.lower())


def _int(value: Any) -> int | None:
    try:
        whole = int(str(value))
    except (TypeError, ValueError):
        return None
    return whole if whole >= 0 else None


def _unsized(rows: list[model.Resource], cause: str) -> None:
    for row in rows:
        row.extra["not_measured"] = cause


def _keep_split(row: model.Resource, active: int | None, long_term: int | None, total: str,
                read: str, out: model.Collect) -> None:
    """Record the age split only if its halves add up to the size on the row; otherwise record the
    disagreement and bill neither half. Absent halves leave the split unobserved."""
    if active is None or long_term is None or row.size_bytes is None:
        return
    if active + long_term != row.size_bytes:
        said = f"active {active} + long-term {long_term} != {total} {row.size_bytes}"
        row.extra["split_disagrees"] = said
        out.refused(read, (f"byte split refused, neither tier taken: {said} "
                           f"[{row.extra.get('dataset')}/{row.name}]")[:200], row.project)
        return
    row.extra["active_logical_bytes"] = active
    row.extra["long_term_logical_bytes"] = long_term
    row.extra.pop("split_disagrees", None)


# ---------------------------------------------------------------- TABLE_STORAGE

def _storage_info(project: str, location: str, rows: list[model.Resource],
                  out: model.Collect) -> dict[tuple[str, str], dict[str, Any]]:
    """One TABLE_STORAGE read, as {(dataset, table): {column: cell}}. Empty with a gap when the read
    did not answer; empty without one when it answered and listed none of these tables."""
    def decline(reason: str) -> dict[tuple[str, str], dict[str, Any]]:
        out.refused(READ_QUERY, reason[:200], project)
        _unsized(rows, READ_REFUSED)
        return {}

    try:
        payload = guard.session().request(
            READ_QUERY, {"projectId": project},
            body={"query": table_storage_sql(project, location), "useLegacySql": False,
                  "maximumBytesBilled": str(MAX_BYTES_BILLED)})
    except guard.AuditError as exc:
        return decline(str(getattr(exc, "reason", None) or exc))

    names = [str(field.get("name") or "")
             for field in ((payload.get("schema") or {}).get("fields") or [])]
    if not all(column in names for column in TABLE_STORAGE_COLUMNS):
        return decline("answered without the named columns")
    if payload.get("jobComplete") is not True:
        return decline("job did not complete, so no rows were read")
    truncated = bool(payload.get("pageToken"))
    if truncated:
        # `jobs.getQueryResults` is not allowlisted, so the rows that answered are used and the read
        # is labelled incomplete.
        out.refused(READ_QUERY,
                    "result truncated: a page token is outstanding and this build cannot page it",
                    project)

    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cell_row in payload.get("rows") or []:
        entry = dict(zip(names, [cell.get("v") for cell in (cell_row.get("f") or [])],
                         strict=False))
        key = (str(entry.get("table_schema") or ""), str(entry.get("table_name") or ""))
        if all(key):
            found.setdefault(key, []).append(entry)
    unique = {key: entries[0] for key, entries in found.items() if len(entries) == 1}
    for row in rows:
        key = _key(row)
        if len(found.get(key, ())) > 1:
            row.extra["not_measured"] = NAME_AMBIGUOUS
        elif truncated and key not in unique:
            row.extra["not_measured"] = TRUNCATED
    return unique


def _key(row: model.Resource) -> tuple[str, str]:
    return (str(row.extra.get("dataset") or ""), row.name)


def _apply_storage_info(row: model.Resource, entry: dict[str, Any], out: model.Collect) -> None:
    logical = _int(entry.get("total_logical_bytes"))
    if logical is None:
        row.extra["not_measured"] = NO_BYTES        # e.g. a view: no size is not zero bytes
    else:
        row.size_bytes = logical
        row.size_source = "TABLE_STORAGE"
        row.extra.pop("not_measured", None)
        _keep_split(row, _int(entry.get("active_logical_bytes")),
                    _int(entry.get("long_term_logical_bytes")), "total_logical_bytes",
                    READ_QUERY, out)
    raw = entry.get(VIEW_DATE_COLUMN)
    date = model.date_only(raw)
    if date:
        row.last_activity, row.activity_field = date, f"TABLE_STORAGE.{VIEW_DATE_COLUMN}"
    elif raw not in (None, ""):
        # Named rather than dropped, so "no date" and "a date we could not parse" print differently.
        row.extra["date_unparsed"] = f"{VIEW_DATE_COLUMN} {str(raw)[:40]}"


# ---------------------------------------------------------------- tables.get

def _size_from_get(project: str, rows: list[model.Resource], out: model.Collect) -> None:
    """One metadata read per row, bounded by half the request budget left and by
    `MAX_GET_REFUSALS`. The row is admitted through the allowlist before it is sent, so a table id
    the guard cannot address costs one gap rather than the run."""
    read = methods.lookup(READ_TABLE)
    budget = (guard.MAX_REQUESTS - guard.stats()["requests"]) // 2
    refusals = asked = 0
    stopped_by: str | None = None
    left_from = len(rows)
    for index, row in enumerate(rows):
        if refusals >= MAX_GET_REFUSALS:
            stopped_by, left_from = "refusals", index
            break
        if asked >= budget:
            stopped_by, left_from = "budget", index
            break
        dataset = str(row.extra.get("dataset") or "")
        if not dataset:
            out.refused(READ_TABLE,
                        f"{row.name}: no dataset id on the row, so no read was sent"[:200], project)
            continue
        params = {"projectId": project, "datasetId": dataset, "tableId": row.name}
        unadmitted = read.admits(params=params, query=None, body=None)
        if unadmitted:
            out.refused(READ_TABLE, f"{dataset}/{row.name}: {unadmitted}"[:200], project)
            continue
        asked += 1
        try:
            payload = guard.session().request(READ_TABLE, params)
        except guard.AuditError as exc:
            refusals += 1
            reason = str(getattr(exc, "reason", None) or exc)
            # Reason first: the ids may be long enough for the cut to reach them.
            out.refused(READ_TABLE, f"metadata read refused, no size taken: {reason}"
                                    f" [{dataset}/{row.name}]"[:200], project)
            continue
        reference = payload.get("tableReference") or {}
        if reference and (str(reference.get("datasetId") or "") != dataset
                          or str(reference.get("tableId") or "") != row.name):
            said = f"{reference.get('datasetId')}/{reference.get('tableId')}"
            out.refused(READ_TABLE, ("metadata answered for another table, so no size was taken: "
                                     f"asked {dataset}/{row.name}, answered {said}")[:200], project)
            continue
        logical = _int(payload.get("numBytes"))
        if logical is None:
            row.extra["not_measured"] = NO_BYTES_GET
            continue
        row.size_bytes = logical
        row.size_source = "bigquery.tables.get/numBytes"
        total = "numBytes"
        if (whole := _int(payload.get(SPLIT_TOTAL_FIELD))) is not None:
            total = f"numBytes (numTotalLogicalBytes {whole})"
        _keep_split(row, _int(payload.get(SPLIT_ACTIVE_FIELD)),
                    _int(payload.get(SPLIT_LONG_TERM_FIELD)), total, READ_TABLE, out)
        # `not_measured` stays: the row still has no `storage_last_modified_time`.
    if stopped_by is None:
        return
    left = len(rows) - left_from
    many = left != 1
    never = (f"{left} more table{'s' if many else ''} {'were' if many else 'was'} never asked "
             f"about, so {'they are' if many else 'it is'} unsized, not measured-empty")
    if stopped_by == "budget":
        for straggler in rows[left_from:]:
            straggler.extra["not_measured"] = NOT_ASKED_BUDGET
        reads = "read" if asked == 1 else "reads"
        out.refused(READ_TABLE,
                    f"request budget spent after {asked} {reads}; {never}"[:200], project)
    else:
        out.refused(READ_TABLE, f"gave up after {refusals} refusals; {never}"[:200], project)


# ---------------------------------------------------------------- enumeration

def _table_rows(project: str, dataset_id: str, location: str,
                out: model.Collect) -> list[model.Resource]:
    """One dataset's tables. A refusal is a gap on this dataset only."""
    rows: list[model.Resource] = []
    try:
        for payload in guard.paged(READ_TABLES, params={"projectId": project,
                                                        "datasetId": dataset_id}):
            for table in (payload.get("tables") or []):
                reference = table.get("tableReference") or {}
                created = model.date_only(table.get("creationTime"))
                table_id = str(reference.get("tableId") or "")
                row = model.Resource(project=project, service=SERVICE, kind="Table",
                                     name=table_id or model.UNKNOWN,
                                     location=location, last_activity=created,
                                     activity_field="creationTime" if created else None)
                row.extra["dataset"] = dataset_id
                if not table_id:
                    row.extra["not_measured"] = NO_TABLE_ID
                rows.append(row)
    except guard.ReadFailed as exc:
        out.refused(READ_TABLES, f"{dataset_id}: {exc.reason}"[:200], project)
    return rows


def collect_bigquery(project: str, *, read_storage_info: bool = False) -> model.Collect:
    out = model.Collect(service=SERVICE)
    datasets: list[tuple[str, str]] = []
    try:
        for payload in guard.paged(READ_DATASETS, params={"projectId": project}):
            for dataset in (payload.get("datasets") or []):
                reference = dataset.get("datasetReference") or {}
                dataset_id = str(reference.get("datasetId") or "")
                if not dataset_id:
                    # No id means no read can address its tables; `NA` is a valid id, never sent.
                    out.refused(READ_DATASETS, "a dataset answered with no datasetId, so its "
                                               "tables were not listed", project)
                    continue
                datasets.append((dataset_id, str(dataset.get("location") or "")))
            unreachable = payload.get("unreachable") or []
            if unreachable:
                out.refused(READ_DATASETS,
                            ("unreachable locations: "
                             + ", ".join(str(x) for x in unreachable))[:200], project)
    except guard.ReadFailed as exc:
        out.refused(READ_DATASETS, exc.reason, project)
        return out

    by_location: dict[str, list[model.Resource]] = {}
    unreadable: list[str] = []
    for dataset_id, location in datasets:
        rows = _table_rows(project, dataset_id, location, out)
        out.resources.extend(rows)
        # A table with no id keeps its row and is never sized: no read can address it.
        rows = [row for row in rows if row.extra.get("not_measured") != NO_TABLE_ID]
        if not rows:
            continue
        if not REGION_RE.match(location):
            _unsized(rows, LOCATION_UNREADABLE)
            unreadable.append(dataset_id)
        elif read_storage_info:
            by_location.setdefault(location.lower(), []).extend(rows)
        else:
            _unsized(rows, NOT_READ)

    if read_storage_info and unreadable:
        out.refused(READ_QUERY, (LOCATION_UNREADABLE + ": " + ", ".join(unreadable))[:200], project)
    for location, rows in sorted(by_location.items()):
        sized = _storage_info(project, location, rows, out)
        for row in rows:
            entry = sized.get(_key(row))
            if entry is not None:
                _apply_storage_info(row, entry, out)
            else:
                row.extra.setdefault("not_measured", NO_BYTES)
    _size_from_get(project, [row for row in out.resources
                             if row.extra.get("not_measured") in SIZED_BY_GET], out)
    return out
