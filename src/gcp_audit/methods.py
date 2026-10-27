"""methods.py — the literal allowlist: every request this toolkit is able to make.

An unknown method is a refusal, and **a verb is not a semantic category**: billing-account `move`
is a GET that mutates and `bigquery.jobs.query` is a POST that reads, so admission is by exact
method id, never by verb.

Callers name a method id and parameters and never compose a URL; each placeholder is filled from a
named regex that forbids `/`, `?`, `#` and `..`. Adding a read is a reviewed edit here, with its
evidence in `docs/API-EVIDENCE.md`; `tests/test_methods.py` pins the count.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from gcp_audit import model


class MethodNotAllowed(Exception):
    """Layer 3. Something asked for a request that is not on this page. Refuse."""


# ---------------------------------------------------------------- parameters

#: Named regexes for path placeholders, deliberately tighter than Google's own patterns.
PATTERNS: dict[str, str] = {
    "project": model.PROJECT_ID_RE.pattern,
    "bucket": r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$",
    "dataset": r"^[a-zA-Z0-9_]{1,1024}$",
    "table": r"^[a-zA-Z0-9_\-@$]{1,1024}$",
    "service": r"^services/[a-zA-Z0-9\-]{1,32}$",
    "project_parent": r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]$",
}

#: Placeholder name -> pattern. Others must be declared per method, or the request is refused.
DEFAULT_PATTERN_OF = {"project": "project", "projectId": "project", "bucket": "bucket",
                      "datasetId": "dataset", "tableId": "table"}

#: Query parameters admitted for any method; anything else is listed per method.
COMMON_PARAMS = frozenset({"pageSize", "maxResults", "pageToken", "returnPartialSuccess",
                           "fields", "alt"})


def _param_ok(name: str, value: str, pattern: str) -> bool:
    if ".." in value:
        return False
    return re.match(pattern, str(value)) is not None


@dataclass(frozen=True)
class Read:
    """One admissible request: id, host, verb, path template, and what may vary."""

    method_id: str
    host: str
    verb: str                       # what the API declares; descriptive, never the test
    path: str                       # template, placeholders in braces
    path_params: tuple[str, ...]    # placeholder names, in template order
    scopes: tuple[str, ...]         # narrow scopes this read is known to work with
    why: str                        # why this is a read, and which field proves it
    query_params: frozenset[str] = frozenset()
    body_params: frozenset[str] = frozenset()   # non-empty only for the POST reads
    patterns: dict[str, str] = field(default_factory=dict)   # placeholder -> PATTERNS key

    def url_of(self, params: dict[str, str]) -> str:
        """The URL for these path parameters; raises if a name is unknown or a value off pattern."""
        if set(params) != set(self.path_params):
            raise MethodNotAllowed(
                f"{self.method_id} needs exactly {self.path_params}, got {sorted(params)}")
        values = {}
        for name in self.path_params:
            value = str(params[name])
            key = self.patterns.get(name) or DEFAULT_PATTERN_OF.get(name)
            if key is None:
                raise MethodNotAllowed(
                    f"{self.method_id}: placeholder {name!r} has no declared value pattern")
            if not _param_ok(name, value, PATTERNS[key]):
                raise MethodNotAllowed(
                    f"{self.method_id}: {name}={value!r} is not shaped like a {name} id")
            values[name] = value
        return f"https://{self.host}/" + self.path.format(**values)

    def admits(self, *, params: dict[str, str], query: dict[str, object] | None,
               body: dict[str, object] | None) -> str:
        """Return "" or the reason this request is refused. Checked before the wire, always."""
        try:
            self.url_of(params)
        except MethodNotAllowed as exc:
            return str(exc)
        allowed = self.query_params | COMMON_PARAMS
        for key in (query or {}):
            if key not in allowed:
                return f"{self.method_id}: query parameter {key!r} is not admitted"
        if body is not None:
            if not self.body_params:
                return (f"{self.method_id}: this read takes no body; a body is a write-shaped "
                        f"request and this method is not declared one")
            for key in body:
                if key not in self.body_params:
                    return f"{self.method_id}: body key {key!r} is not admitted"
        return ""


# ------------------------------------------------------------------ the table

def _r(*args, **kwargs) -> Read:      # keeps the table readable at 100 columns
    return Read(*args, **kwargs)


READS: dict[str, Read] = {r.method_id: r for r in [
    _r("cloudresourcemanager.projects.list", "cloudresourcemanager.googleapis.com", "GET",
       "v3/projects", (), ("cloud-platform.read-only",),
       "Enumerates projects (ids, numbers, state) to resolve a run's scope. `parent` serves "
       "`--folder`; `showDeleted` is deliberately not admitted.",
       query_params=frozenset({"parent"})),
    _r("cloudresourcemanager.projects.get", "cloudresourcemanager.googleapis.com", "GET",
       "v3/{name}", ("name",), ("cloud-platform.read-only",),
       "One project by name, which verifies a typed id. Its "
       "`state` decides what is ACTIVE.",
       patterns={"name": "project_parent"}),
    _r("cloudresourcemanager.projects.search", "cloudresourcemanager.googleapis.com", "GET",
       "v3/projects:search", (), ("cloud-platform.read-only",),
       "Every project the caller can `resourcemanager.projects.get`, which is what `--all` and a "
       "bare `--label` need (crm v3 rev 20260917). `query` ORs multiple fields, so only a single "
       "`labels.<key>:<value>` term is sent and state and label are re-checked on each row.",
       query_params=frozenset({"query"})),
    _r("serviceusage.services.list", "serviceusage.googleapis.com", "GET",
       "v1/{parent}/services", ("parent",), ("cloud-platform.read-only",),
       "Which APIs a project has enabled — the only way to state which services this pass did "
       "NOT enumerate. v1/{parent}/services is a collection read.",
       query_params=frozenset({"filter"}), patterns={"parent": "project_parent"}),
    _r("compute.instances.aggregatedList", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/aggregated/instances", ("project",),
       ("compute.readonly",),
       "One GET for every VM in every zone. Fields read: machineType, status, zone, "
       "creationTimestamp, lastStartTimestamp, disks[].diskSizeGb."),
    _r("compute.disks.aggregatedList", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/aggregated/disks", ("project",),
       ("compute.readonly",),
       "Persistent disks: sizeGb (already the billing unit), status, lastAttachTimestamp, "
       "users[]. Attached disks and free disks both come from here."),
    _r("compute.images.list", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/global/images", ("project",), ("compute.readonly",),
       "Custom images: name, status and creationTimestamp, the only date they carry. The API "
       "gives no billed size, so images are listed unsized."),
    _r("compute.snapshots.list", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/global/snapshots", ("project",), ("compute.readonly",),
       "Snapshots: storageBytes with storageBytesStatus (LIKELY_INCOMPLETE means the size is a "
       "floor), never diskSizeGb, which is the source disk's size and overstates by the dedup."),
    _r("compute.machineTypes.aggregatedList", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/aggregated/machineTypes", ("project",),
       ("compute.readonly",),
       "guestCpus and memoryMb per machine type. An instance reports a machine type URL, not a "
       "core count, and a VM is priced per core and per GB of RAM."),
    _r("compute.addresses.aggregatedList", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/aggregated/addresses", ("project",), ("compute.readonly",),
       "Regional reserved IP addresses: status (RESERVED = not attached), addressType, users[], "
       "region, creationTimestamp. An unattached external address bills by the hour."),
    _r("compute.globalAddresses.list", "compute.googleapis.com", "GET",
       "compute/v1/projects/{project}/global/addresses", ("project",), ("compute.readonly",),
       "Global reserved IP addresses, same `Address` fields as the regional read."),
    _r("storage.buckets.list", "storage.googleapis.com", "GET",
       "storage/v1/b", (), ("cloud-platform.read-only", "devstorage.read_only"),
       "Buckets for a project: name, location, storageClass, timeCreated, updated. `updated` is a "
       "metadata change, which is why the table names the field a date came from.",
       query_params=frozenset({"project", "prefix", "projection", "softDeleted"})),
    _r("storage.objects.list", "storage.googleapis.com", "GET",
       "storage/v1/b/{bucket}/o", ("bucket",),
       ("cloud-platform.read-only", "devstorage.read_only"),
       "One `maxResults=1` page of a gauge-silent bucket's object names: an empty page with no "
       "`nextPageToken` is a measured zero. Names only, never bytes; no other query key admitted.",),
    _r("monitoring.projects.timeSeries.list", "monitoring.googleapis.com", "GET",
       "v3/{name}/timeSeries", ("name",), ("monitoring.read",),
       "The gauge reads: a bucket's byte count (storage/total_bytes) and nothing else. A metric "
       "that emits nothing is NO_SERIES, which is not the same statement as zero bytes.",
       query_params=frozenset({"filter", "interval.startTime", "interval.endTime",
                               "aggregation.alignmentPeriod", "aggregation.perSeriesAligner",
                               "aggregation.crossSeriesReducer", "aggregation.groupByFields",
                               "secondaryAggregation.alignmentPeriod",
                               "secondaryAggregation.perSeriesAligner", "orderBy", "view"}),
       patterns={"name": "project_parent"}),
    # BigQuery v2 declares no read-only scope of its own, so these four ride
    # `cloud-platform.read-only`.
    _r("bigquery.datasets.list", "bigquery.googleapis.com", "GET",
       "bigquery/v2/projects/{projectId}/datasets", ("projectId",),
       ("cloud-platform.read-only",),
       "Datasets in a project: datasetReference and location. The list view carries no size and "
       "no timestamps.",
       query_params=frozenset({"all", "filter"})),
    _r("bigquery.tables.list", "bigquery.googleapis.com", "GET",
       "bigquery/v2/projects/{projectId}/datasets/{datasetId}/tables", ("projectId", "datasetId"),
       ("cloud-platform.read-only",),
       "Tables in a dataset: tableReference, type, creationTime. No numBytes; sizes come from "
       "`tables.get` or TABLE_STORAGE.",
       query_params=frozenset({"filter"})),
    _r("bigquery.tables.get", "bigquery.googleapis.com", "GET",
       "bigquery/v2/projects/{projectId}/datasets/{datasetId}/tables/{tableId}",
       ("projectId", "datasetId", "tableId"),
       ("cloud-platform.read-only",),
       "One table's metadata: `numBytes` and the active/long-term split. Sizes tables without "
       "`--read-bq-storage`, at no slot cost; returns no table data."),
    _r("bigquery.jobs.query", "bigquery.googleapis.com", "POST",
       "bigquery/v2/projects/{projectId}/queries", ("projectId",),
       ("cloud-platform.read-only",),
       "The one POST: a constant SELECT on INFORMATION_SCHEMA.TABLE_STORAGE, for every table's "
       "size, age split and storage_last_modified_time in one location. Gated behind "
       "`--read-bq-storage` because a query costs the project slot time.",
       body_params=frozenset({"query", "useLegacySql", "maxResults", "timeoutMs", "location",
                              "maximumBytesBilled", "requestId", "kind", "dryRun"})),
    _r("cloudbilling.services.list", "cloudbilling.googleapis.com", "GET",
       "v1/services", (), ("cloud-billing.readonly",),
       "The service ids to ask for price SKUs of. This API honours only cloud-billing.readonly, "
       "not cloud-platform.read-only."),
    _r("cloudbilling.services.skus.list", "cloudbilling.googleapis.com", "GET",
       "v1/{parent}/skus", ("parent",), ("cloud-billing.readonly",),
       "Dated list prices. The method has no server-side filter, so the sheet is paged in full.",
       query_params=frozenset({"startTime", "endTime", "currencyCode"}),
       patterns={"parent": "service"}),
]}

#: Reads that spend the project's slot time: only behind their named flag, never by default.
GATED: dict[str, str] = {"bigquery.jobs.query": "--read-bq-storage"}

#: Pinned by `tests/test_methods.py`, so adding a read is a deliberate edit.
READ_COUNT = len(READS)


def lookup(method_id: str) -> Read:
    read = READS.get(method_id)
    if read is None:
        raise MethodNotAllowed(
            f"{method_id!r} is not on the allowlist in src/gcp_audit/methods.py "
            f"({READ_COUNT} reads are); refusing to send it")
    return read


_PLACEHOLDER = re.compile(r"\{\+?([A-Za-z]+)\}")


def _compiled(read: Read) -> re.Pattern:
    """The concrete-path regex for a template, placeholders bounded as in `url_of`, so the built
    URL is checked a second time."""
    parts = _PLACEHOLDER.split(read.path)      # [pre, name, mid, name, ..., tail]
    out: list[str] = [re.escape(parts[0])]
    for i in range(1, len(parts), 2):
        name = parts[i]
        key = read.patterns.get(name) or DEFAULT_PATTERN_OF.get(name)
        if key is None:
            raise MethodNotAllowed(
                f"{read.method_id}: placeholder {name!r} has no declared pattern")
        out.append("(" + PATTERNS[key][1:-1] + ")")
        out.append(re.escape(parts[i + 1]))
    return re.compile("^" + "".join(out) + "$")


COMPILED: dict[str, re.Pattern] = {r.method_id: _compiled(r) for r in READS.values()}


def match(verb: str, url: str) -> Read | None:
    """Which allowlisted read, if any, produces this exact URL with this verb."""
    host, _, path = url.partition("//")[2].partition("/")
    for read in READS.values():
        if read.host == host and read.verb == str(verb).upper() and COMPILED[read.method_id].match(path):
            return read
    return None
