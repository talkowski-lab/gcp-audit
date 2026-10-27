"""collect_storage.py — GCS buckets: `storage.buckets.list` for rows, one gauge read for size.

* `buckets.list` has no size, so bytes come from one `monitoring` gauge read per project. A bucket
  the gauge said nothing about is NO_SERIES with `size_bytes` None: a gauge with no series is not
  zero bytes. A gauge that answered 0 is an observed zero.
* A bucket's bytes are the SUM of each of its series' freshest point: the gauge answers one series
  per distinguishing label, and one series alone understates a multi-class bucket.
* A gauge-silent bucket gets one `objects.list` page of one object. An empty, finished page means
  no live object versions, a measured 0. The listing is never summed, and it is skipped when
  `versioning.enabled` is true, because non-current versions are billed and not listed. Retention,
  TTL, holds and soft-deleted objects stay invisible to it (`docs/DESIGN.md` §Sizing and money).
* `updated` is a METADATA timestamp: a lifecycle edit refreshes it with nobody reading an object.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gcp_audit import guard, model

SERVICE = "storage.googleapis.com"
READ_BUCKETS = "storage.buckets.list"
READ_GAUGE = "monitoring.projects.timeSeries.list"
READ_OBJECTS = "storage.objects.list"

#: The byte gauge, and the `gcs_bucket` label a byte count is attributable to.
METRIC_TOTAL_BYTES = "storage.googleapis.com/storage/total_bytes"
BUCKET_LABEL = "bucket_name"

#: `view` is required by this method; omitting it would arrive as NO_SERIES.
VIEW_FULL = "FULL"

#: Seven days, not one: the gauge is sampled once a day with a delay, so a 24-hour window answers
#: "no series" for buckets that have bytes. `tests/test_collect_storage.py` pins this floor.
GAUGE_WINDOW = timedelta(days=7)

#: What `extra["not_measured"]` says; neither is a zero. NO_SERIES names its window, so a week of
#: silence is not read as a lifetime of it.
NO_SERIES = f"byte gauge asked about {GAUGE_WINDOW.days} days emitted no series"
GAUGE_REFUSED = "byte gauge read refused"

#: One object name answers "is this bucket empty"; never a second page.
OBJECT_PROBE_PAGE = 1

#: Probe outcomes that do NOT size a bucket, in `extra["objects_probe"]`, worded by
#: `pricing.OBJECT_PROBE_REASONS`. An empty, finished page sizes it at 0 instead.
PROBE_OCCUPIED = "occupied"
PROBE_UNFINISHED = "unfinished"
PROBE_VERSIONED = "versioned"
PROBE_REFUSED = "refused"


def _versioning_enabled(bucket: dict[str, Any]) -> bool:
    """True only when the `versioning` block arrived with `enabled` true. An absent block is the
    API's "not set, and versioning disabled", and the zero is granted on that reading."""
    block = bucket.get("versioning")
    return isinstance(block, dict) and bool(block.get("enabled"))


def _settle_by_listing(project: str, row: model.Resource, *, versioned: bool,
                       out: model.Collect) -> None:
    """Size one gauge-silent bucket at 0 only if versioning is off and one page comes back with no
    entries, no `prefixes` and no `nextPageToken`. Every other outcome leaves it unobserved."""
    row.extra["not_measured"] = NO_SERIES
    if versioned:
        row.extra["objects_probe"] = PROBE_VERSIONED
        return
    try:
        payload = guard.session().request(READ_OBJECTS, params={"bucket": row.name},
                                         query={"maxResults": OBJECT_PROBE_PAGE})
    except guard.ReadFailed as exc:
        out.refused(READ_OBJECTS, f"{row.name}: {exc.reason}"[:200], project)
        row.extra["objects_probe"] = PROBE_REFUSED
        return
    if [item for item in payload.get("items") or [] if isinstance(item, dict)]:
        row.extra["objects_probe"] = PROBE_OCCUPIED
        return
    if payload.get("nextPageToken") or payload.get("prefixes"):
        row.extra["objects_probe"] = PROBE_UNFINISHED
        return
    row.size_bytes = 0
    row.size_source = READ_OBJECTS
    row.extra.pop("not_measured", None)


def _rfc3339(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def gauge_query(now: datetime) -> dict[str, str]:
    """The gauge query over `GAUGE_WINDOW`; `now` is a parameter so a test can pin the interval."""
    return {
        "filter": f'metric.type="{METRIC_TOTAL_BYTES}"',
        "interval.startTime": _rfc3339(now - GAUGE_WINDOW),
        "interval.endTime": _rfc3339(now),
        "view": VIEW_FULL,
    }


def _whole_bytes(typed_value: dict[str, Any]) -> int | None:
    """A `TypedValue` as whole bytes. A `distributionValue` or an unparsable number is None."""
    for key in ("int64Value", "doubleValue"):
        if key in typed_value:
            try:
                return int(float(typed_value[key]))
            except (TypeError, ValueError):
                return None
    return None


def _bytes_by_bucket(payloads: list[dict[str, Any]]) -> dict[str, int]:
    """Sum each series' freshest point per bucket. A series naming no bucket is skipped."""
    freshest: dict[tuple, tuple[str, int]] = {}
    for payload in payloads:
        for series in payload.get("timeSeries") or []:
            resource_labels = ((series.get("resource") or {}).get("labels") or {})
            bucket = str(resource_labels.get(BUCKET_LABEL) or "")
            if not bucket:
                continue
            metric_labels = (series.get("metric") or {}).get("labels") or {}
            key = (bucket, tuple(sorted(metric_labels.items())),
                   tuple(sorted((k, str(v)) for k, v in resource_labels.items()
                                if k != BUCKET_LABEL)))
            for point in series.get("points") or []:
                whole = _whole_bytes(point.get("value") or {})
                # RFC 3339 `Z` stamps sort correctly as text.
                end = str((point.get("interval") or {}).get("endTime") or "")
                if whole is None or whole < 0 or not end:
                    continue
                seen = freshest.get(key)
                if seen is None or end > seen[0]:
                    freshest[key] = (end, whole)
    totals: dict[str, int] = {}
    for (bucket, _metric, _resource), (_end, whole) in freshest.items():
        totals[bucket] = totals.get(bucket, 0) + whole
    return totals


def _sizes(project: str, out: model.Collect) -> dict[str, int] | None:
    """One gauge read for the project. None is a refused read (a gap); an empty map is the answer
    "no bucket emitted a series"."""
    payloads: list[dict[str, Any]] = []
    try:
        for payload in guard.paged(READ_GAUGE, params={"name": f"projects/{project}"},
                                   query=gauge_query(datetime.now(UTC))):
            payloads.append(payload)
    except guard.ReadFailed as exc:
        out.refused(READ_GAUGE, exc.reason, project)
        return None
    return _bytes_by_bucket(payloads)


def _bucket_row(project: str, bucket: dict[str, Any]) -> model.Resource:
    updated = model.date_only(bucket.get("updated"))
    row = model.Resource(project=project, service=SERVICE, kind="Bucket",
                         name=str(bucket.get("name") or model.UNKNOWN),
                         location=str(bucket.get("location") or ""),
                         last_activity=updated,
                         activity_field="updated" if updated else None)
    if bucket.get("storageClass"):
        row.extra["storage_class"] = str(bucket["storageClass"])
    return row


def collect_storage(project: str) -> model.Collect:
    """Buckets as rows, sized by the byte gauge, or by one object page where it was silent."""
    out = model.Collect(service=SERVICE)
    listed: list[dict[str, Any]] = []
    try:
        for payload in guard.paged(READ_BUCKETS, params={}, query={"project": project}):
            listed.extend(item for item in (payload.get("items") or []) if isinstance(item, dict))
            unreachable = payload.get("unreachable") or []
            if unreachable:
                # An unreachable bucket makes the listing a floor; the table says so.
                out.refused(READ_BUCKETS,
                            ("unreachable: " + ", ".join(str(n) for n in unreachable))[:200],
                            project)
    except guard.ReadFailed as exc:
        out.refused(READ_BUCKETS, exc.reason, project)
        return out

    out.resources.extend(_bucket_row(project, bucket) for bucket in listed)
    # Versioning decides how a bucket may be sized; it is not a printed fact, so not in `extra`.
    versioned = {str(bucket.get("name") or model.UNKNOWN): _versioning_enabled(bucket)
                 for bucket in listed}

    sizes = _sizes(project, out)
    for row in out.resources:
        if sizes is None:
            row.extra["not_measured"] = GAUGE_REFUSED
        elif row.name in sizes:
            row.size_bytes = sizes[row.name]
            row.size_source = METRIC_TOTAL_BYTES
        else:
            _settle_by_listing(project, row, versioned=versioned.get(row.name, False), out=out)
    return out
