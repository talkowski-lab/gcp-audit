"""The compute collector: what each kind of row records, and what it must leave unobserved.

`fake_session.install` overrides only `_dispatch`, so every request still runs the real admission
code. Payload shapes follow `docs/API-EVIDENCE.md`: `items` as a scope map for an aggregatedList,
int64s as strings, and URLs in `zone`, `machineType` and `users`.
"""
from __future__ import annotations

import pytest
from fake_session import install

from gcp_audit import guard, methods, model
from gcp_audit.collect_compute import collect_compute

PROJECT = "lab-seq-prd"
BASE = "https://www.googleapis.com/compute/v1/projects/lab-seq-prd"
ZONE = f"{BASE}/zones/us-central1-a"
MT = f"{ZONE}/machineTypes/e2-standard-4"


def instance(name="web-1", **over):
    return {"kind": "compute#instance", "name": name, "id": "9001", "machineType": MT,
            "status": "RUNNING", "zone": ZONE, "selfLink": f"{ZONE}/instances/{name}",
            "creationTimestamp": "2026-01-05T09:12:33.000-08:00",
            "lastStartTimestamp": "2026-09-30T02:11:00.000-07:00", **over}


def disk(name="web-1", **over):
    return {"kind": "compute#disk", "name": name, "id": "8001", "sizeGb": "100",
            "status": "IN_USE", "zone": ZONE, "selfLink": f"{ZONE}/disks/{name}",
            "type": f"{ZONE}/diskTypes/pd-ssd",
            "users": [f"{ZONE}/instances/web-1"],
            "creationTimestamp": "2026-01-05T09:12:33.000-08:00",
            "lastAttachTimestamp": "2026-09-28T10:00:00.000-07:00", **over}


def machine_type(name="e2-standard-4", **over):
    return {"kind": "compute#machineType", "name": name, "zone": ZONE, "guestCpus": 4,
            "memoryMb": 16384, "isSharedCpu": False, "selfLink": f"{ZONE}/machineTypes/{name}",
            **over}


def image(name="debian-12-20260101", **over):
    return {"kind": "compute#image", "name": name, "id": "7001", "status": "READY",
            "selfLink": f"{BASE}/global/images/{name}",
            "creationTimestamp": "2026-01-01T02:00:00.000-08:00", "diskSizeGb": "10", **over}


def snapshot(name="snap-web-1", **over):
    return {"kind": "compute#snapshot", "name": name, "id": "6001", "status": "READY",
            "selfLink": f"{BASE}/global/snapshots/{name}",
            "sourceDisk": f"{ZONE}/disks/web-1", "storageBytes": "41231686016",
            "storageBytesStatus": "UP_TO_DATE", "diskSizeGb": "100",
            "creationTimestamp": "2026-09-01T01:30:00.000-07:00", **over}


def aggregated(inner_key, scopes):
    """`{items: {scope: {inner_key: [...]}}}`, the shape an aggregatedList answers in."""
    return {"kind": "compute#list", "id": f"{PROJECT}#list",
            "items": {scope: body for scope, body in scopes.items()}}


def routes(instances=None, disks=None, images=None, snapshots=None, machine_types=None,
           addresses=None, global_addresses=None):
    return {
        "compute.addresses.aggregatedList": aggregated(
            "addresses", {"regions/us-central1": {"addresses": addresses or []}}),
        "compute.globalAddresses.list": {"kind": "compute#addressList",
                                         "items": global_addresses or []},
        "compute.machineTypes.aggregatedList": aggregated(
            "machineTypes", {"zones/us-central1-a": {"machineTypes": machine_types
                                                     if machine_types is not None
                                                     else [machine_type()]}}),
        "compute.instances.aggregatedList": aggregated(
            "instances", {"zones/us-central1-a": {"instances": instances or []}}),
        "compute.disks.aggregatedList": aggregated(
            "disks", {"zones/us-central1-a": {"disks": disks or []}}),
        "compute.images.list": {"kind": "compute#imageList", "items": images or []},
        "compute.snapshots.list": {"kind": "compute#snapshotList", "items": snapshots or []},
    }


def resources(found, kind):
    return [res for res in found.resources if res.kind == kind]


# ------------------------------------------------------------------------ happy path

def test_one_row_per_kind_with_the_fields_each_read_observes():
    install(routes(instances=[instance()], disks=[disk()], images=[image()],
                   snapshots=[snapshot()]))
    found = collect_compute(PROJECT)

    assert found.service == "compute.googleapis.com"
    assert found.gaps == []
    assert {res.kind for res in found.resources} == {"Instance", "Disk", "Image", "Snapshot"}
    assert all(res.project == PROJECT and res.service == "compute.googleapis.com"
               for res in found.resources)

    vm = resources(found, "Instance")[0]
    assert (vm.name, vm.state, vm.location) == ("web-1", "RUNNING", "us-central1-a")
    assert vm.cpus == 4 and vm.memory_mb == 16384
    assert vm.last_activity == "2026-09-30" and vm.activity_field == "lastStartTimestamp"
    assert vm.evidence()["machine_type"] == "e2-standard-4"

    pd = resources(found, "Disk")[0]
    assert pd.state == "IN_USE"
    assert pd.size_gb_api == 100 and pd.size_bytes is None       # never multiplied into bytes
    assert pd.size_source == "compute.disks.aggregatedList"
    assert pd.last_activity == "2026-09-28" and pd.activity_field == "lastAttachTimestamp"
    assert pd.extra["attached_to"] == "web-1"
    assert pd.evidence()["storage_class"] == "pd-ssd"

    img = resources(found, "Image")[0]
    assert (img.location, img.state) == ("global", "READY")
    assert img.last_activity == "2026-01-01" and img.activity_field == "creationTimestamp"

    snap = resources(found, "Snapshot")[0]
    assert snap.size_bytes == 41231686016
    assert snap.size_gb_api is None                              # never sized off diskSizeGb
    assert snap.evidence()["gib"] == round(41231686016 / model.GIB, 2)
    assert snap.evidence()["source_disk"] == "web-1"
    assert "storage_bytes_status" not in snap.evidence()                     # UP_TO_DATE is not a floor


def test_seven_reads_and_no_eighth():
    fake = install(routes())
    collect_compute(PROJECT)
    assert fake.count("compute.instances.aggregatedList") == 1
    assert {call[0] for call in fake.calls} == {
        "compute.instances.aggregatedList", "compute.disks.aggregatedList",
        "compute.images.list", "compute.snapshots.list",
        "compute.machineTypes.aggregatedList", "compute.addresses.aggregatedList",
        "compute.globalAddresses.list"}
    assert guard.stats()["requests"] == 7
    assert fake.queries("compute.images.list")[0] == {"maxResults": 500}


def address(name, status="RESERVED", **over):
    return {"kind": "compute#address", "name": name, "status": status, "addressType": "EXTERNAL",
            "region": f"{BASE}/regions/us-central1",
            "creationTimestamp": "2025-03-04T10:00:00.000-08:00", **over}


def test_external_addresses_are_rows_and_internal_ones_are_not():
    install(routes(addresses=[address("idle"),
                              address("web", "IN_USE", users=[f"{ZONE}/instances/web-1"]),
                              address("private", addressType="INTERNAL"),
                              address("default-type", addressType=None)],
                   global_addresses=[address("lb", region=None, networkTier="PREMIUM")]))
    rows = {r.name: r for r in resources(collect_compute(PROJECT), "Address")}

    assert sorted(rows) == ["default-type", "idle", "lb", "web"]
    assert (rows["idle"].state, rows["idle"].location) == ("RESERVED", "us-central1")
    assert (rows["idle"].last_activity, rows["idle"].activity_field) == (
        "2025-03-04", "creationTimestamp")
    assert rows["web"].extra["attached_to"] == "web-1"
    assert rows["lb"].location == "global" and rows["lb"].extra["network_tier"] == "PREMIUM"


# ---------------------------------------------------------------- the machine-type join

def test_a_machine_type_absent_from_the_list_is_not_measured_never_guessed():
    install(routes(instances=[instance(machineType=f"{ZONE}/machineTypes/weird-9")],
                   machine_types=[machine_type()]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]
    assert vm.cpus is None and vm.memory_mb is None
    assert vm.extra["not_measured"] == "machine type not listed"
    assert "cpus" not in vm.evidence() and "memory_mb" not in vm.evidence()


def test_a_partial_machine_type_url_joins_the_same_as_a_full_one():
    install(routes(instances=[instance(machineType="zones/us-central1-a/machineTypes/"
                                                 "e2-standard-4")]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]
    assert (vm.cpus, vm.memory_mb) == (4, 16384)
    assert "not_measured" not in vm.extra


def test_a_refused_machine_type_read_does_not_become_absent_from_the_list():
    """A failed machine-type read is reported as a failed read, not as a type missing from the
    list."""
    fake = install(routes(instances=[instance()]),
                   failures={"compute.machineTypes.aggregatedList":
                             (403, "PERMISSION_DENIED compute.readonly is not enough")})
    found = collect_compute(PROJECT)
    vm = resources(found, "Instance")[0]
    assert vm.cpus is None and vm.memory_mb is None
    assert vm.extra["not_measured"] == "machine-type read failed"
    assert vm.extra["machine_type"] == "e2-standard-4"           # the instance still named it
    assert fake.count("compute.machineTypes.aggregatedList") == 1
    assert [gap.read for gap in found.gaps] == ["compute.machineTypes.aggregatedList"]


def test_a_stopped_instance_is_not_zeroed_and_its_disk_is_still_sized():
    install(routes(instances=[instance(status="STOPPED", lastStartTimestamp=None)],
                   disks=[disk()]))
    found = collect_compute(PROJECT)
    vm, pd = resources(found, "Instance")[0], resources(found, "Disk")[0]
    assert vm.state == "STOPPED" and (vm.cpus, vm.memory_mb) == (4, 16384)
    assert vm.last_activity == "2026-01-05" and vm.activity_field == "creationTimestamp"
    assert pd.size_gb_api == 100


# ------------------------------------------------------------------------- the disk dates

def test_an_unattached_disk_has_no_date_at_all():
    """`creationTimestamp` must not stand in for last use, or "created and never used" would look
    like "used recently"."""
    install(routes(disks=[disk(name="orphan-backup", status="READY", users=[])]))
    pd = resources(collect_compute(PROJECT), "Disk")[0]
    assert pd.last_activity is None and pd.activity_field is None
    assert "attached_to" not in pd.extra
    assert pd.size_gb_api == 100                      # unattached is still billed, still a row
    assert model.cells(model.Row.of(pd, None, model.Unpriced("UNPRICED", "x")))[11] == "NA"


def test_an_attached_disk_with_no_attach_record_still_has_no_date():
    install(routes(disks=[disk(lastAttachTimestamp=None)]))
    pd = resources(collect_compute(PROJECT), "Disk")[0]
    assert pd.last_activity is None and pd.extra["attached_to"] == "web-1"


def test_a_disk_names_its_class_where_pricing_reaches_for_it():
    """Disk classes differ several-fold in price, so the row carries `storage_class`: the basename
    of `disks[].type`, whether the API sent a URL or a bare name."""
    unnamed = disk(name="mystery")
    unnamed.pop("type")
    install(routes(disks=[disk(name="from-url", type=f"{ZONE}/diskTypes/pd-balanced"),
                          disk(name="from-name", type="pd-balanced"),
                          disk(name="null-class", type=None),
                          unnamed]))
    found = collect_compute(PROJECT)
    classes = {res.name: res.evidence().get("storage_class", "absent")
               for res in resources(found, "Disk")}
    assert classes == {"from-url": "pd-balanced", "from-name": "pd-balanced",
                       "null-class": "absent", "mystery": "absent"}


def test_a_disk_whose_class_was_never_named_carries_no_class_key_at_all():
    """An unobserved class leaves no key, not an empty one. Pricing must refuse the disk rather than
    assume `pd-standard`."""
    unnamed = disk(name="mystery", status="READY", users=[])
    unnamed.pop("type")
    install(routes(disks=[unnamed]))
    pd = resources(collect_compute(PROJECT), "Disk")[0]
    assert "storage_class" not in pd.extra and "storage_class" not in pd.evidence()
    assert pd.size_gb_api == 100                       # still a row, still sized, still a refusal
    line = model.cells(model.Row.of(pd, None, model.Unpriced("UNPRICED",
                                                             "disk class not observed")))
    assert "disk class not observed" in line[-1] and "storage_class" not in line[-1]


# ------------------------------------------------------------- sizes, floors and honesty

def test_a_likely_incomplete_snapshot_says_its_size_is_a_floor():
    install(routes(snapshots=[snapshot(storageBytesStatus="LIKELY_INCOMPLETE")]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.size_bytes == 41231686016
    assert snap.extra["storage_bytes_status"] == "LIKELY_INCOMPLETE"
    assert snap.evidence()["storage_bytes_status"] == "LIKELY_INCOMPLETE"


def test_an_updating_snapshot_is_a_floor_too_because_the_size_is_still_moving():
    install(routes(snapshots=[snapshot(storageBytesStatus="UPDATING")]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.extra["storage_bytes_status"] == "UPDATING"


def test_an_image_is_not_sized_off_what_the_image_api_actually_reports():
    """`images.list` gives `diskSizeGb` (size after restore) and no billed byte count at revision
    20260922. Using either would print a size nobody measured, so the image is `UNSIZED`."""
    install(routes(images=[image()]))
    img = resources(collect_compute(PROJECT), "Image")[0]
    assert img.size_bytes is None and img.size_gb_api is None
    assert img.size_source == ""
    assert "bytes" not in img.evidence() and "size_gb" not in img.evidence()


def test_a_snapshot_sized_off_storage_bytes_ignores_the_source_disk_size_beside_it():
    install(routes(snapshots=[snapshot(storageBytes="1073741824", diskSizeGb="10000")]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.size_bytes == 1073741824 and snap.size_gb_api is None


def test_a_snapshot_carries_the_storage_place_that_decides_its_rate():
    """`storageLocations` is where the snapshot's bytes are stored, and pricing uses it to pick a
    rate. The row's `location` stays `global`. `["us"]` is the value seen on real snapshots
    (`docs/API-EVIDENCE.md` §Snapshots and images)."""
    install(routes(snapshots=[snapshot(storageLocations=["us"])]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.location == "global"
    assert snap.extra["storage_locations"] == "us"
    assert snap.evidence()["storage_locations"] == "us"


def test_a_snapshot_whose_bytes_sit_in_two_places_says_both():
    """Both entries are kept, in arrival order, unsorted and not de-duplicated. Pricing refuses a
    row with two places and names them.

    The pair is synthetic: no live reply with two entries has been seen.
    """
    install(routes(snapshots=[snapshot(storageLocations=["US", "EU"])]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.extra["storage_locations"] == "US,EU"
    assert snap.evidence()["storage_locations"] == "US,EU"


def test_a_snapshot_whose_storageLocations_arrived_empty_is_an_answer_not_a_silence():
    """An empty array is recorded as `storage_locations_status=answered-empty`.

    `Resource.evidence()` drops falsy values, so without the status key an empty array would look
    the same as a missing field. Only the collector can tell the two apart.
    """
    install(routes(snapshots=[snapshot(storageLocations=[])]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.extra["storage_locations_status"] == "answered-empty"
    assert "storage_locations" not in snap.extra
    assert snap.evidence()["storage_locations_status"] == "answered-empty"


def test_a_snapshot_with_no_storageLocations_recorded_nothing_at_all():
    """A missing `storageLocations` records nothing. `region` applies only to regional snapshots
    and `locationHint` is for internal tools, so neither is used as a substitute."""
    install(routes(snapshots=[snapshot(locationHint="us", region=None)]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert "storage_locations" not in snap.extra and "storage_locations_status" not in snap.extra
    assert "storage_locations" not in snap.evidence()
    assert snap.location == "global"


def test_a_regional_snapshot_is_located_at_its_region_not_at_the_url_that_carries_it():
    """`Snapshot.region` is a URL, so the row's location is its basename (`us-central1`)."""
    install(routes(snapshots=[snapshot(region=f"{BASE}/regions/us-central1")]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.location == "us-central1"


def test_an_int64_that_is_not_a_number_leaves_the_size_unobserved_not_zero():
    install(routes(disks=[disk(sizeGb="one hundred")], snapshots=[snapshot(
        storageBytes="")]))
    found = collect_compute(PROJECT)
    assert resources(found, "Disk")[0].size_gb_api is None
    assert resources(found, "Snapshot")[0].size_bytes is None


def test_an_unparseable_timestamp_never_becomes_1970():
    install(routes(snapshots=[snapshot(creationTimestamp="0")]))
    snap = resources(collect_compute(PROJECT), "Snapshot")[0]
    assert snap.last_activity is None and snap.activity_field is None


def test_a_resource_the_payload_would_not_name_keeps_its_row_and_shows_a_question_mark():
    install(routes(images=[{"kind": "compute#image", "status": "READY"}]))
    img = resources(collect_compute(PROJECT), "Image")[0]
    assert img.name == model.UNKNOWN


# ------------------------------------------------------------------------------- paging

def test_two_pages_are_two_pages_of_one_universe():
    fake = install(routes())
    fake.routes["compute.snapshots.list"] = [
        {"kind": "compute#snapshotList", "items": [snapshot(name="snap-a")],
         "nextPageToken": "TOKEN-2"},
        {"kind": "compute#snapshotList", "items": [snapshot(name="snap-b")],
         "selfLink": f"{BASE}/global/snapshots"},
    ]
    found = collect_compute(PROJECT)
    assert [res.name for res in resources(found, "Snapshot")] == ["snap-a", "snap-b"]
    assert found.gaps == []
    assert fake.count("compute.snapshots.list") == 2
    assert fake.queries("compute.snapshots.list")[1]["pageToken"] == "TOKEN-2"


def test_a_read_that_runs_out_of_pages_keeps_its_rows_and_labels_the_set_partial(monkeypatch):
    """At the page cap `guard.paged` raises, and the collector keeps the rows it read and adds a
    gap. The real `guard.paged` is wrapped only to set a one-page cap."""
    real_paged = guard.paged

    def one_page(method_id, *, params, query=None, body=None):
        return real_paged(method_id, params=params, query=query, body=body, max_pages=1)

    monkeypatch.setattr(guard, "paged", one_page)
    fake = install(routes())
    fake.routes["compute.images.list"] = [
        {"kind": "compute#imageList", "items": [image(name="img-a")], "nextPageToken": "T2"},
        {"kind": "compute#imageList", "items": [image(name="img-b")], "nextPageToken": "T3"},
    ]
    found = collect_compute(PROJECT)
    assert [res.name for res in resources(found, "Image")] == ["img-a"]
    assert [gap.read for gap in found.gaps] == ["compute.images.list"]
    assert "incomplete" in found.gaps[0].reason
    assert fake.count("compute.images.list") == 1


# ------------------------------------------------------------------------- refusal as gap

def test_a_403_is_a_gap_that_names_the_read_and_the_reason_and_keeps_the_other_rows():
    install(routes(instances=[instance()], images=[image()], snapshots=[snapshot()]),
            failures={"compute.disks.aggregatedList":
                      (403, "PERMISSION_DENIED Insufficient authentication scopes")})
    found = collect_compute(PROJECT)
    assert found.gaps == [model.Gap(project=PROJECT, service="compute.googleapis.com",
                                    read="compute.disks.aggregatedList",
                                    reason="PERMISSION_DENIED Insufficient authentication "
                                           "scopes")]
    assert {res.kind for res in found.resources} == {"Instance", "Image", "Snapshot"}
    assert found.resources and found.gaps         # never an empty list, never a silent zero


def test_every_failing_read_gets_its_own_gap():
    install(routes(), failures={"compute.images.list": (404, "NOT_FOUND nope"),
                                "compute.snapshots.list": (500, "INTERNAL backend trouble")})
    found = collect_compute(PROJECT)
    assert [(gap.read, gap.reason) for gap in found.gaps] == [
        ("compute.images.list", "NOT_FOUND nope"),
        ("compute.snapshots.list", "INTERNAL backend trouble")]


def test_an_empty_project_is_an_answer_and_not_a_gap():
    install(routes())
    found = collect_compute(PROJECT)
    assert (found.resources, found.gaps) == ([], [])


def test_a_guard_refusal_is_not_swallowed_as_a_cloud_condition():
    """`methods.MethodNotAllowed` is our own refusal, not a cloud condition, so it escapes the
    collector instead of becoming a gap."""
    install(routes())
    with pytest.raises(methods.MethodNotAllowed):
        collect_compute("not a project id")


# ------------------------------------------------------------------- partial results

def test_a_partial_success_warning_is_a_gap_not_a_smaller_project():
    """A `PARTIAL_SUCCESS` warning keeps the rows that came back and adds a gap, since the list is
    not the whole project. The collector never sets `returnPartialSuccess`."""
    fake = install(routes(disks=[disk()]))
    fake.routes["compute.disks.aggregatedList"] = {
        "kind": "compute#diskAggregatedList",
        "warning": {"code": "PARTIAL_SUCCESS",
                    "message": "If partialSuccess is true, result contains a partial list",
                    "data": [{"key": "PARTIAL_RESULT", "value": "true"}]},
        "items": {"zones/us-central1-a": {"disks": [disk()]}},
    }
    found = collect_compute(PROJECT)
    assert len(resources(found, "Disk")) == 1
    assert [gap.read for gap in found.gaps] == ["compute.disks.aggregatedList"]
    assert "PARTIAL_SUCCESS" in found.gaps[0].reason
    assert "the whole list" in found.gaps[0].reason
    assert all("returnPartialSuccess" not in (query or {})
               for query in fake.queries("compute.disks.aggregatedList"))


def test_a_zone_that_declined_is_named_in_the_gap():
    fake = install(routes(instances=[instance()]))
    fake.routes["compute.instances.aggregatedList"] = aggregated("instances", {
        "zones/us-central1-a": {"instances": [instance()]},
        "zones/us-east1-d": {"warning": {
            "code": "NOT_CRITICAL_ERROR",
            "message": "The response is missing instances from unreachable zone",
            "data": [{"key": "scope", "value": "zones/us-east1-d"}]}},
    })
    found = collect_compute(PROJECT)
    assert len(resources(found, "Instance")) == 1
    assert len(found.gaps) == 1
    assert found.gaps[0].reason.startswith("partial list (zones/us-east1-d): "
                                           "zones/us-east1-d NOT_CRITICAL_ERROR")


def test_an_empty_zone_is_an_answer_rather_than_a_failure():
    """`NO_RESULTS_ON_PAGE` on a scope means the zone has nothing, so it is not a gap. Most zones
    in a healthy project answer this way."""
    fake = install(routes(instances=[instance()]))
    fake.routes["compute.instances.aggregatedList"] = aggregated("instances", {
        "zones/us-central1-a": {"instances": [instance()]},
        "zones/us-east1-d": {"warning": {"code": "NO_RESULTS_ON_PAGE",
                                         "message": "There are no results on the page",
                                         "data": [{"key": "scope", "value": "zones/us-east1-d"}]}},
    })
    found = collect_compute(PROJECT)
    assert len(resources(found, "Instance")) == 1
    assert found.gaps == []


# ------------------------------------------------------------------ custom machine types
#
# `machineTypes.aggregatedList` lists only predefined types, so a custom VM is sized from its name:
# `<family>-custom-<vCPUs>-<MB>[-<modifier>]`. `n2d-custom-2-24832-ext` comes from a real VM; the
# other custom names are made up for the shapes under test.


def custom_vm(name: str, machine_type: str):
    return instance(name=name, machineType=f"{ZONE}/machineTypes/{machine_type}")


def test_a_custom_machine_type_is_sized_from_its_name_when_the_table_does_not_list_it():
    """`n2d-custom-2-24832-ext` gives 2 vCPUs and 24,832 MB. The name is a field the instances read
    returned, so this is an observation, not an estimate."""
    install(routes(instances=[custom_vm("vm-custom-ext", "n2d-custom-2-24832-ext")],
                   machine_types=[]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]

    assert (vm.cpus, vm.memory_mb) == (2, 24832)
    assert "not_measured" not in vm.extra
    evidence = vm.evidence()
    assert evidence["machine_type"] == "n2d-custom-2-24832-ext"
    assert evidence["cpus"] == 2 and evidence["memory_mb"] == 24832


def test_the_machine_type_read_still_wins_when_the_table_lists_the_shape():
    """If the machine-type list includes a custom type, its `guestCpus` and `memoryMb` win. The
    name is only a fallback."""
    install(routes(instances=[custom_vm("listed-custom", "n2d-custom-2-24832-ext")],
                   machine_types=[machine_type(name="n2d-custom-2-24832-ext", guestCpus=4,
                                               memoryMb=8192)]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]

    assert (vm.cpus, vm.memory_mb) == (4, 8192)


@pytest.mark.parametrize("machine_type", [
    "weird-9",                    # a name with no shape in it
    "n2d-custom-2",               # a truncated shape: no memory figure to read
    "n2d-custom-2-abcmem",        # a memory figure that is not a number
    "custom-2-24832",             # no family prefix, so no rate table to price it in
    "n2d-custom-2-24832-ext-2",   # a tail this grammar does not describe
    "e2-medium", "n1-standard-8",  # predefined names: the table has those, the parser must not
])
def test_a_machine_type_name_that_does_not_parse_leaves_the_counts_unobserved_not_zero(machine_type):
    """A name that does not parse leaves the counts as `None` and the row says `not_measured`.
    Guessed counts would bill for a VM nobody has."""
    install(routes(instances=[custom_vm("odd", machine_type)], machine_types=[]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]

    assert (vm.cpus, vm.memory_mb) == (None, None)
    assert vm.extra["not_measured"] == "machine type not listed"
    assert "cpus" not in vm.evidence() and "memory_mb" not in vm.evidence()


def test_a_refused_machine_type_read_still_prices_a_custom_shape_from_its_name():
    """With the machine-type read refused, a custom shape is still sized from its name. A
    predefined type has no other source, so it says "read failed"."""
    install(routes(instances=[custom_vm("vm-custom-ext", "n2d-custom-2-24832-ext"),
                              instance(name="web-1")]),
            failures={"compute.machineTypes.aggregatedList":
                      (403, "PERMISSION_DENIED compute.readonly is not enough")})
    rows = {res.name: res for res in resources(collect_compute(PROJECT), "Instance")}

    assert (rows["vm-custom-ext"].cpus, rows["vm-custom-ext"].memory_mb) == (2, 24832)
    assert "not_measured" not in rows["vm-custom-ext"].extra
    assert rows["web-1"].cpus is None
    assert rows["web-1"].extra["not_measured"] == "machine-type read failed"


# ------------------------------------------- what a VM bills beyond cores and RAM


def test_gpus_spot_and_local_ssds_are_recorded_on_the_instance_row():
    install(routes(instances=[instance(
        "gpu-1",
        guestAccelerators=[{"acceleratorType": f"{ZONE}/acceleratorTypes/nvidia-tesla-t4",
                            "acceleratorCount": 2}],
        scheduling={"provisioningModel": "SPOT"},
        disks=[{"type": "PERSISTENT", "boot": True}, {"type": "SCRATCH"}, {"type": "SCRATCH"}])]))
    vm = resources(collect_compute(PROJECT), "Instance")[0]

    assert vm.extra["gpus"] == "2" and vm.extra["gpu_type"] == "nvidia-tesla-t4"
    assert vm.extra["provisioning_model"] == "SPOT"
    assert vm.extra["local_ssd"] == "2"
    evidence = vm.evidence()
    assert (evidence["gpus"], evidence["provisioning_model"], evidence["local_ssd"]) == (
        "2", "SPOT", "2")


def test_a_legacy_preemptible_flag_is_recorded_and_a_standard_vm_records_nothing():
    install(routes(instances=[instance("old", scheduling={"preemptible": True}),
                              instance("plain", scheduling={"provisioningModel": "STANDARD",
                                                            "preemptible": False})]))
    by_name = {vm.name: vm for vm in resources(collect_compute(PROJECT), "Instance")}

    assert by_name["old"].extra["provisioning_model"] == "PREEMPTIBLE"
    for key in ("gpus", "provisioning_model", "local_ssd", "shared_cpu"):
        assert key not in by_name["plain"].extra


def test_a_machine_types_built_in_gpus_and_shared_core_are_recorded():
    install(routes(instances=[instance("a2", machineType=f"{ZONE}/machineTypes/a2-highgpu-1g"),
                              instance("micro", machineType=f"{ZONE}/machineTypes/e2-micro")],
                   machine_types=[machine_type("a2-highgpu-1g", guestCpus=12, memoryMb=87040,
                                               accelerators=[
                                                   {"guestAcceleratorType": "nvidia-tesla-a100",
                                                    "guestAcceleratorCount": 1}]),
                                  machine_type("e2-micro", guestCpus=2, memoryMb=1024,
                                               isSharedCpu=True)]))
    by_name = {vm.name: vm for vm in resources(collect_compute(PROJECT), "Instance")}

    assert (by_name["a2"].extra["gpus"], by_name["a2"].extra["gpu_type"]) == (
        "1", "nvidia-tesla-a100")
    assert by_name["micro"].extra["shared_cpu"] == "true"
