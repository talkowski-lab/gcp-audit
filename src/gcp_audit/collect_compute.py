"""collect_compute.py — instances, disks, images, snapshots and external IP addresses.

* An unattached disk has no date: `creationTimestamp` never stands in for an access.
* `cpus`/`memory_mb` come from the machine-type read, or from a custom type's own name; a name that
  does not parse stays unobserved rather than becoming a guess.
* A `warning` in a list payload is a gap, not a smaller project (`NO_RESULTS_ON_PAGE` excepted).
* A snapshot is sized on `storageBytes`, never `diskSizeGb`.
* Only EXTERNAL reserved addresses are rows: internal ones are not billed.

No money is produced here; every dollar is `pricing.py`'s.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from gcp_audit import guard, model

SERVICE = "compute.googleapis.com"

_INSTANCE_READ = "compute.instances.aggregatedList"
_DISK_READ = "compute.disks.aggregatedList"
_IMAGE_READ = "compute.images.list"
_SNAPSHOT_READ = "compute.snapshots.list"
_MACHINE_READ = "compute.machineTypes.aggregatedList"
_ADDRESS_READ = "compute.addresses.aggregatedList"
_GLOBAL_ADDRESS_READ = "compute.globalAddresses.list"

#: The documented ceiling for `maxResults`, so a page size does not depend on a server default.
MAX_RESULTS = 500

#: `storageLocations` arrived and named no place. A stated answer, because an empty `extra` value
#: prints nowhere and would look the same as a reply that never carried the field.
ANSWERED_EMPTY = "answered-empty"

#: Warning codes that are an answer, not a declination: compute emits `NO_RESULTS_ON_PAGE` for
#: every empty zone. Every other code is the service withholding part of the answer.
ANSWER_CODES = frozenset({"NO_RESULTS_ON_PAGE"})

#: A custom machine type's name carries its shape (`n2d-custom-2-24832-ext` is 2 vCPUs, 24,832 MB)
#: and `machineTypes.aggregatedList` never lists it. The tail is ignored here; `-ext` matters to
#: pricing (`pricing.EXTENDED_MARKER`).
_CUSTOM_MACHINE_TYPE = re.compile(
    r"^(?P<family>[a-z0-9]+)-custom-(?P<cpus>\d+)-(?P<memory_mb>\d+)(?:-(?P<modifier>[a-z]+))?$")


def custom_shape(machine_type: str) -> tuple[int, int] | None:
    """`(cpus, memory_mb)` out of a custom machine-type name, or None when it is not one.

    Never `(0, 0)`. Predefined names (`n1-standard-8`) do not match; their counts come from the
    machine-type read.
    """
    found = _CUSTOM_MACHINE_TYPE.match((machine_type or "").strip())
    if found is None:
        return None
    cpus, memory_mb = _count(found["cpus"]), _count(found["memory_mb"])
    if cpus is None or memory_mb is None:
        return None
    return cpus, memory_mb


def _machine_key(url: object, zone: object) -> str:
    """`<zone>/<machine-type-name>`: `instances.machineType` is a full or partial URL and a catalog
    entry keeps its zone in a separate field, so neither side joins on a raw string."""
    parts = [p for p in str(url or "").split("/") if p]
    name = parts[-1] if parts else ""
    if len(parts) >= 3 and parts[-2] == "machineTypes":
        return f"{parts[-3]}/{name}"
    return f"{_short(zone)}/{name}"


def collect_compute(project: str) -> model.Collect:
    """Every instance, disk, image and snapshot in one project, plus what could not be read.

    Empty `resources` with empty `gaps` means the project has none; beside a gap it means nothing.
    """
    out = model.Collect(service=SERVICE)
    machines = _machine_types(project, out)
    for payload in _pages(_INSTANCE_READ, project, out):
        for _scope, item in _scoped(payload, "instances"):
            out.resources.append(_instance(item, project, machines))
    for payload in _pages(_DISK_READ, project, out):
        for _scope, item in _scoped(payload, "disks"):
            out.resources.append(_disk(item, project))
    for payload in _pages(_IMAGE_READ, project, out):
        for item in _flat(payload):
            out.resources.append(_image(item, project))
    for payload in _pages(_SNAPSHOT_READ, project, out):
        for item in _flat(payload):
            out.resources.append(_snapshot(item, project))
    for payload in _pages(_ADDRESS_READ, project, out):
        out.resources.extend(row for _scope, item in _scoped(payload, "addresses")
                             if (row := _address(item, project)) is not None)
    for payload in _pages(_GLOBAL_ADDRESS_READ, project, out):
        out.resources.extend(row for item in _flat(payload)
                             if (row := _address(item, project)) is not None)
    return out


# ------------------------------------------------------------------------ the reads

def _pages(read: str, project: str, out: model.Collect) -> Iterator[dict[str, Any]]:
    """Every page of one read; a `guard.ReadFailed` becomes one gap and the rows already collected
    stay. `methods.MethodNotAllowed` is not caught: our own refusal is not a cloud condition."""
    try:
        for payload in guard.paged(read, params={"project": project},
                                   query={"maxResults": MAX_RESULTS}):
            _note_warnings(read, payload, project, out)
            yield payload
    except guard.ReadFailed as exc:
        out.refused(read, exc.reason, project)


def _machine_types(project: str, out: model.Collect) -> dict[str, dict[str, Any]] | None:
    """The catalog by `<zone>/<name>`, or None when the read failed: "never read" and "no such
    type" are different sentences on a VM's row."""
    index: dict[str, dict[str, Any]] = {}
    for payload in _pages(_MACHINE_READ, project, out):
        for scope, item in _scoped(payload, "machineTypes"):
            zone = _short(item.get("zone")) or _short(scope)
            index.setdefault(_machine_key(item.get("selfLink") or item.get("name"), zone), item)
    if any(gap.read == _MACHINE_READ for gap in out.gaps):
        return None
    return index


def _note_warnings(read: str, payload: dict[str, Any], project: str, out: model.Collect) -> None:
    """One gap per `warning` in a payload, top level and per scope, keeping the rows that came.
    `returnPartialSuccess` is never sent, so a warning is the service saying its answer is short."""
    found: list[tuple[str, Any]] = [("the whole list", payload.get("warning"))]
    items = payload.get("items")
    if isinstance(items, dict):       # an aggregatedList: each scope can decline on its own
        found += [(scope, block.get("warning"))
                  for scope, block in items.items() if isinstance(block, dict)]
    for where, warning in found:
        if not isinstance(warning, dict):
            continue
        code = str(warning.get("code") or "")
        if code in ANSWER_CODES:
            continue
        out.refused(read, f"partial list ({where}): {_say(warning)}"[:200], project)


def _say(warning: dict[str, Any]) -> str:
    """The payload's own words for a warning: code, message, and the scope in `warning.data`."""
    data = {str(entry.get("key")): str(entry.get("value"))
            for entry in warning.get("data") or [] if isinstance(entry, dict)}
    reason = " ".join(str(part).strip() for part in
                      (warning.get("code"), warning.get("message")) if part)
    scope = data.get("scope") or ""
    return f"{scope} {reason}" if scope else reason or "warning without a reason"


# ------------------------------------------------------------------------ the rows

def _instance(item: dict[str, Any], project: str,
              machines: dict[str, dict[str, Any]] | None) -> model.Resource:
    machine = str(item.get("machineType") or "")
    row = model.Resource(
        project=project, service=SERVICE, kind="Instance", name=_name(item),
        location=_short(item.get("zone")), state=str(item.get("status") or ""),
        extra={"machine_type": _short(machine)} if machine else {},
    )
    _dated(row, item, ("lastStartTimestamp", "creationTimestamp"))
    _billing_modifiers(row, item)
    found = machines.get(_machine_key(machine, row.location)) if machines is not None else None
    if found is not None:
        row.cpus = _count(found.get("guestCpus"))
        row.memory_mb = _count(found.get("memoryMb"))
        if found.get("isSharedCpu") is True:
            row.extra["shared_cpu"] = "true"
        _add_accelerators(row, found.get("accelerators"))
        return row
    # Custom shapes are never in the machine-type table; their name carries both counts.
    shape = custom_shape(_short(machine))
    if shape is not None:
        row.cpus, row.memory_mb = shape
        return row
    row.extra["not_measured"] = ("machine-type read failed" if machines is None
                                 else "machine type not listed")
    return row


def _billing_modifiers(row: model.Resource, item: dict[str, Any]) -> None:
    """GPUs, a Spot or preemptible provisioning model, and local SSDs (`SCRATCH` disks, which
    `disks.aggregatedList` does not list)."""
    _add_accelerators(row, item.get("guestAccelerators"))
    scheduling = item.get("scheduling") or {}
    model_name = str(scheduling.get("provisioningModel") or "")
    if scheduling.get("preemptible") is True and model_name in ("", "STANDARD"):
        model_name = "PREEMPTIBLE"
    if model_name and model_name != "STANDARD":
        row.extra["provisioning_model"] = model_name
    scratch = sum(1 for disk in item.get("disks") or []
                  if isinstance(disk, dict) and disk.get("type") == "SCRATCH")
    if scratch:
        row.extra["local_ssd"] = str(scratch)


def _add_accelerators(row: model.Resource, accelerators: object) -> None:
    """Sum `acceleratorCount` over `guestAccelerators` (attached) or a machine type's built-in
    `accelerators` (a2, g2, ...), recording the type names alongside."""
    total = int(row.extra.get("gpus") or 0)
    kinds = [k for k in str(row.extra.get("gpu_type") or "").split(model.EXTRA_LIST_SEP) if k]
    for entry in accelerators if isinstance(accelerators, list) else []:
        if not isinstance(entry, dict):
            continue
        count = _count(entry.get("acceleratorCount", entry.get("guestAcceleratorCount")))
        kind = _short(entry.get("acceleratorType") or entry.get("guestAcceleratorType"))
        if count:
            total += count
            if kind and kind not in kinds:
                kinds.append(kind)
    if total:
        row.extra["gpus"] = str(total)
        row.extra["gpu_type"] = model.EXTRA_LIST_SEP.join(kinds) or model.UNKNOWN


def _disk(item: dict[str, Any], project: str) -> model.Resource:
    users = [str(user) for user in item.get("users") or [] if user]
    extra: dict[str, str] = {}
    if users:
        extra["attached_to"] = _short(users[0])
    disk_class = _short(item.get("type"))
    if disk_class:
        # `pd-standard` and `pd-ssd` are several-fold apart: an unnamed class stays absent.
        extra["storage_class"] = disk_class
    size = _count(item.get("sizeGb"))
    row = model.Resource(
        project=project, service=SERVICE, kind="Disk", name=_name(item),
        location=_short(item.get("zone")) or _short(item.get("region")),
        state=str(item.get("status") or ""),
        size_gb_api=size,          # `sizeGb` is already the billing unit; never convert it
        size_source=_DISK_READ if size is not None else "",
        extra=extra,
    )
    if users:                      # unattached: no access record exists, so no date is printed
        _dated(row, item, ("lastAttachTimestamp",))
    return row


def _address(item: dict[str, Any], project: str) -> model.Resource | None:
    """An EXTERNAL reserved address, or None for an INTERNAL one (not billed). `addressType`
    defaults to EXTERNAL when absent, per the API. `status` RESERVED means nothing uses it."""
    if str(item.get("addressType") or "EXTERNAL") != "EXTERNAL":
        return None
    extra: dict[str, str] = {}
    users = [_short(user) for user in item.get("users") or [] if user]
    if users:
        extra["attached_to"] = ",".join(users)
    for field, key in (("networkTier", "network_tier"), ("ipVersion", "ip_version")):
        if item.get(field):
            extra[key] = str(item[field])
    row = model.Resource(
        project=project, service=SERVICE, kind="Address", name=_name(item),
        location=_short(item.get("region")) or "global", state=str(item.get("status") or ""),
        extra=extra)
    _dated(row, item, ("creationTimestamp",))
    return row


def _image(item: dict[str, Any], project: str) -> model.Resource:
    """Usually unsized: `diskSizeGb` and `archiveSizeBytes` are not the storage billed today."""
    row = model.Resource(
        project=project, service=SERVICE, kind="Image", name=_name(item),
        location="global", state=str(item.get("status") or ""),
    )
    _dated(row, item, ("creationTimestamp",))
    _sized(row, item, _IMAGE_READ)
    return row


def _snapshot(item: dict[str, Any], project: str) -> model.Resource:
    row = model.Resource(
        project=project, service=SERVICE, kind="Snapshot", name=_name(item),
        location=_short(item.get("region")) or "global",
        state=str(item.get("status") or ""),
    )
    _dated(row, item, ("creationTimestamp",))
    _sized(row, item, _SNAPSHOT_READ)
    status = str(item.get("storageBytesStatus") or "")
    if status and status != "UP_TO_DATE":
        # Anything but UP_TO_DATE makes `storageBytes` a floor; one key, named after the API field.
        row.extra["storage_bytes_status"] = status
    if item.get("sourceDisk"):
        row.extra["source_disk"] = _short(item.get("sourceDisk"))
    _storage_places(row, item)
    return row


def _storage_places(row: model.Resource, item: dict[str, Any]) -> None:
    """`storageLocations` verbatim, which decides the snapshot rate table (`pricing._snapshot`).

    No field writes nothing; an empty array is `ANSWERED_EMPTY`; places keep the API's order and
    repeats, so a doubled entry is never collapsed into a price.
    """
    listed = item.get("storageLocations")
    if not isinstance(listed, list):
        return
    places = [each for each in listed if isinstance(each, str) and each]
    if places:
        row.extra["storage_locations"] = model.EXTRA_LIST_SEP.join(places)
    else:
        row.extra["storage_locations_status"] = ANSWERED_EMPTY


# ---------------------------------------------------------------- field-level helpers

def _sized(row: model.Resource, item: dict[str, Any], read: str) -> None:
    """`storageBytes` when present; `diskSizeGb` beside it is never the billed size."""
    size = _count(item.get("storageBytes"))
    if size is not None:
        row.size_bytes, row.size_source = size, read


def _dated(row: model.Resource, item: dict[str, Any], fields: tuple[str, ...]) -> None:
    """First field that parses to a date wins and names itself in `activity_field`. An unparseable
    field yields nothing: a `NA` beats a cell naming a field it has no date from."""
    for field in fields:
        day = model.date_only(item.get(field))
        if day is not None:
            row.last_activity, row.activity_field = day, field
            return


def _scoped(payload: dict[str, Any], key: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """(scope, item) pairs from an aggregatedList payload; scope keys are locations, not items."""
    items = payload.get("items")
    if not isinstance(items, dict):
        return
    for scope, block in items.items():
        if not isinstance(block, dict):
            continue
        for item in block.get(key) or []:
            if isinstance(item, dict):
                yield str(scope), item


def _flat(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`items` of a plain list read (images, snapshots), minus anything not an object."""
    items = payload.get("items")
    return [item for item in items or [] if isinstance(item, dict)]


def _name(item: dict[str, Any]) -> str:
    """The API's own name, or `NA`: an unnamed resource stays in the table rather than vanishing."""
    return str(item.get("name") or "").strip() or model.UNKNOWN


def _short(value: object) -> str:
    """Last path segment of a compute URL: `.../zones/us-central1-a` to `us-central1-a`."""
    text = str(value or "").strip().rstrip("/")
    return text.rsplit("/", 1)[-1] if text else ""


def _count(value: object) -> int | None:
    """A compute int64, sent as a string (`"100"`). Unparseable is None, never 0: `0` is kept for
    a measured zero."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None
