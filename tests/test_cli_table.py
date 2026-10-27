"""End-to-end runs against staged payloads: CLI, guard, collectors, pricing and output files.

Everything is offline. The payloads follow the discovery documents (`docs/API-EVIDENCE.md`), and
`tests/fake_session.py` still enforces the real allowlist. A run must keep its `NA` cost rows and
its `last_activity_field = not-read` rows, not drop them or turn them into zeros.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest
from fake_session import install

from gcp_audit import cli, guard, methods, model
from gcp_audit import collect_bigquery as collect_bigquery_module

PROJECT = "lab-seq-prd"
OTHER = "lab-gen-dev"

MACHINE_TYPE = ("https://www.googleapis.com/compute/v1/projects/lab-seq-prd/zones/"
                "us-central1-a/machineTypes/e2-standard-4")


def instances(project=PROJECT):
    return [{"id": "1", "name": "wdl-worker", "machineType": MACHINE_TYPE,
             "zone": f"https://www.googleapis.com/compute/v1/projects/{project}"
                     "/zones/us-central1-a",
             "status": "RUNNING", "creationTimestamp": "2025-01-05T04:00:00.000-08:00",
             "lastStartTimestamp": "2026-09-27T03:11:00.000-07:00",
             "disks": [{"diskSizeGb": 100, "source": "https://x/disks/pd-worker"}]}]


def disks(project=PROJECT):
    return [{"id": "2", "name": "pd-worker", "sizeGb": "100", "status": "IN_USE",
             "zone": "us-central1-a", "type": "pd-standard",
             "creationTimestamp": "2025-01-05T04:00:00.000-08:00",
             "lastAttachTimestamp": "2026-09-27T03:11:00.000-07:00",
             "users": [f"https://www.googleapis.com/compute/v1/projects/{project}"
                       "/zones/us-central1-a/instances/wdl-worker"]},
            {"id": "3", "name": "pd-forgotten", "sizeGb": "500", "status": "READY",
             "zone": "us-central1-a", "type": "pd-standard",
             "creationTimestamp": "2024-02-01T04:00:00.000-08:00"}]


def machine_types(project=PROJECT):
    return [{"id": "4", "name": "e2-standard-4", "guestCpus": 4, "memoryMb": 16384,
             "zone": "us-central1-a"}]


def images():
    return [{"name": "ubuntu-2004-lts-copy", "creationTimestamp": "2024-06-01T00:00:00.000-07:00",
             "storageBytes": "8589934592", "storageBytesStatus": "STORAGE_BYTES_STATUS_COMPLETE"}]


def snapshots():
    # `storageLocations` names the rate table the bytes bill on; `["us"]` is the US multi-region
    # (`docs/API-EVIDENCE.md` §Snapshots and images). The row's `location` stays `global`, which is
    # what `snapshots.list` reports for a standard snapshot.
    return [{"name": "snap-2024-11", "creationTimestamp": "2024-11-02T00:00:00.000-07:00",
             "storageBytes": "123456789012", "storageLocations": ["us"],
             "storageBytesStatus": "LIKELY_INCOMPLETE", "diskSizeGb": "200"}]


def scoped(items):
    """aggregatedList wraps rows per scope and reports partial failure separately."""
    return {"items": {"zones/us-central1-a": {"instances": items}}}


def disk_scoped(items):
    return {"items": {"zones/us-central1-a": {"disks": items}}}


def mt_scoped(items):
    return {"items": {"zones/us-central1-a": {"machineTypes": items}}}


BUCKETS = {"items": [
    # Single region, so the rate lookup has one answer.
    {"name": "lab-raw", "location": "europe-west1", "storageClass": "NEARLINE",
     "timeCreated": "2023-04-01T00:00:00Z", "updated": "2026-06-30T10:00:00Z"},
    # No gauge series, so the size is unobserved (`UNSIZED`), not zero. The object listing finds an
    # object, so the bucket is not empty either.
    {"name": "lab-tmp", "location": "us-central1", "storageClass": "STANDARD",
     "timeCreated": "2026-01-01T00:00:00Z", "updated": "2026-09-01T00:00:00Z"},
    # Multi-region. `EU` is priced on the area's own record, `Nearline Storage Europe
    # Multi-region`, offered at `europe`: not at `eu` and not at any single region.
    {"name": "lab-eu", "location": "EU", "storageClass": "NEARLINE",
     "timeCreated": "2024-01-01T00:00:00Z", "updated": "2026-08-01T00:00:00Z"}]}

GAUGE = {"timeSeries": [
    {"metric": {"type": "storage.googleapis.com/storage/total_bytes",
                "labels": {"bucket_name": "lab-raw"}},
     "resource": {"type": "gcs_bucket",
                  "labels": {"bucket_name": "lab-raw", "project_id": PROJECT}},
     "points": [{"value": {"doubleValue": 7.4e14},
                 "interval": {"endTime": "2026-10-19T00:00:00Z"}}]},
    {"metric": {"type": "storage.googleapis.com/storage/total_bytes",
                "labels": {"bucket_name": "lab-eu"}},
     "resource": {"type": "gcs_bucket",
                  "labels": {"bucket_name": "lab-eu", "project_id": PROJECT}},
     "points": [{"value": {"doubleValue": 1073741824},
                 "interval": {"endTime": "2026-10-19T00:00:00Z"}}]}]}

# Two datasets: `variants` in a region and `eu_raw` in the `EU` multi-region. `tables.list` is
# answered per dataset, in call order.
DATASETS = {"datasets": [
    {"id": f"{PROJECT}:variants", "location": "us-central1",
     "datasetReference": {"projectId": PROJECT, "datasetId": "variants"}},
    {"id": f"{PROJECT}:eu_raw", "location": "EU",
     "datasetReference": {"projectId": PROJECT, "datasetId": "eu_raw"}}]}
_TABLES_VARIANTS = {"tables": [{"id": f"{PROJECT}:variants.calls",
                               "tableReference": {"projectId": PROJECT, "datasetId": "variants",
                                                  "tableId": "calls"},
                               "type": "TABLE", "creationTime": "1700000000000"}]}
_TABLES_EU_RAW = {"tables": [{"id": f"{PROJECT}:eu_raw.alignments",
                             "tableReference": {"projectId": PROJECT, "datasetId": "eu_raw",
                                                "tableId": "alignments"},
                             "type": "TABLE", "creationTime": "1699000000000"}]}
# The fake hands out one response per `tables.list` call, in order. Two projects with two datasets
# each make four calls. A row's project comes from the request, not the payload.
TABLES = [_TABLES_VARIANTS, _TABLES_EU_RAW] * 2

#: What `tables.get` answers for each table, routed by URL. The halves add up to `numBytes`.
TABLE_GET_ALIGNMENTS = {
    "tableReference": {"projectId": PROJECT, "datasetId": "eu_raw", "tableId": "alignments"},
    "numBytes": "32212254720", "numActiveLogicalBytes": "21474836480",
    "numLongTermLogicalBytes": "10737418240"}
TABLE_GET_CALLS = {
    "tableReference": {"projectId": PROJECT, "datasetId": "variants", "tableId": "calls"},
    "numBytes": "536870912000", "numActiveLogicalBytes": "429496729600",
    "numLongTermLogicalBytes": "107374182400"}


def _sku(service, sku_id, description, family, group, units, nanos, usage_unit, regions,
         start="2026-09-01T00:00:00Z", end="2099-01-01T00:00:00Z"):
    """A SKU shaped like a live record.

    `usageUnit` is the unit code (`h`, `GiBy.h`, `GiBy.mo`), which pricing keys on. The one tier
    uses `units`/`nanos` (nanos are 10^-9 USD), and the pricing period covers today. The `service`
    prefix decides which sheet the record lands on.
    """
    return {"name": f"{service}/skus/{sku_id}", "skuId": sku_id,
            "description": description, "category": {"resourceFamily": family,
                                                     "resourceGroup": group},
            "serviceRegions": regions, "usageUnit": usage_unit,
            "pricingInfo": [{"effectiveTime": start,
                             "pricingExpression": {"usageUnit": usage_unit,
                                                  "tieredRates": [
                                                      {"startUsageAmount": 0,
                                                       "unitPrice": {"currencyCode": "USD",
                                                                     "units": str(units),
                                                                     "nanos": nanos}}]},
                             "pricingPeriod": {"startTime": start, "endTime": end}}]}


COMPUTE_SERVICE = "services/6F81-5844-456A"
STORAGE_SERVICE = "services/9404-1459-3979"
#: The real BigQuery sheet id (`docs/API-EVIDENCE.md` §Cloud Billing Catalog). Other services also
#: have "BigQuery" in their names, so a substring match could pick the wrong one.
BIGQUERY_SERVICE = "services/24E6-581D-38E5"
#: `serviceId` as the API returns it: bare, without the `services/` prefix that `name` carries.
COMPUTE_SERVICE_ID = "6F81-5844-456A"
STORAGE_SERVICE_ID = "9404-1459-3979"
BIGQUERY_SERVICE_ID = "24E6-581D-38E5"

# Rates and `description` strings come from live records (E2 core $0.031611/h, E2 RAM
# $0.004237/GiBy.h, PD Standard $0.04/GiBy.mo, Nearline $0.010/GiBy.mo). `pricing` matches a
# record by the start of its description, so an invented description would price nothing.
COMPUTE_SKUS = {"skus": [
    _sku(COMPUTE_SERVICE, "E2-CORE", "E2 Instance Core running in Americas", "Compute", "CPU",
         0, 31611000, "h", ["us-central1", "americas"]),
    _sku(COMPUTE_SERVICE, "E2-RAM", "E2 Instance Ram running in Americas", "Compute", "RAM",
         0, 4237000, "GiBy.h", ["us-central1", "americas"]),
    _sku(COMPUTE_SERVICE, "PD-STD", "Storage PD Capacity in Americas", "Storage", "PDStandard",
         0, 40000000, "GiBy.mo", ["us-central1", "americas"]),
    # The live snapshot record: `34CF-7D88-5D40` "Storage PD Snapshot in US", `PDSnapshot`, one
    # tier at 0.065 USD per `GiBy.mo`, offered at `us` (`docs/API-EVIDENCE.md` §Snapshots and
    # images). Its `effectiveTime` uses the fixture default so the record is current here.
    _sku(COMPUTE_SERVICE, "34CF-7D88-5D40", "Storage PD Snapshot in US", "Storage", "PDSnapshot",
         0, 65000000, "GiBy.mo", ["us"]),
    _sku(COMPUTE_SERVICE, "EXPIRED", "E2 Instance Core running in Europe", "Compute", "CPU",
         0, 30000000, "h", ["europe-west1"],
         start="2025-01-01T00:00:00Z", end="2025-12-31T00:00:00Z"),
    # No image record, so the image row is `UNPRICED` and names the lookup.
]}
def _free_step(sku: dict, at: int = 10) -> dict:
    """Add a free first tier below a single-rate SKU, the way Google sends an allowance.

    Real BigQuery storage records are 0 GiB -> $0, then 10 GiB -> $0.02. A flat fixture would bill
    the free 10 GiB without any test noticing.
    """
    tiers = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"]
    price = tiers[0]["unitPrice"]
    tiers[0] = {"startUsageAmount": at, "unitPrice": price}
    tiers.insert(0, {"startUsageAmount": 0, "unitPrice": dict(price, units="0", nanos=0)})
    return sku


# The live record `66A2-68EA-56BE` "Static Ip Charge", Network / IpAddress: 0 h -> $0, then
# $0.01 per hour.
COMPUTE_SKUS["skus"].append(
    _free_step(_sku(COMPUTE_SERVICE, "66A2-68EA-56BE", "Static Ip Charge", "Network", "IpAddress",
                    0, 10000000, "h", ["us-central1", "us-east1"]), at=1))


#: The four live records that price a stored table (`docs/API-EVIDENCE.md` §The BigQuery sheet).
#: Both pairs are staged because their rates differ (`[us, europe]` is 0.02 / 0.01, `us-central1`
#: is 0.023 / 0.016), so a row sent to the wrong pair gets a visibly wrong price.
BIGQUERY_SKUS = {"skus": [
    _free_step(_sku(BIGQUERY_SERVICE, "947D-3B46-7781", "Active Logical Storage",
                    "ApplicationServices", "ActiveStorage", 0, 20000000, "GiBy.mo",
                    ["us", "europe"])),
    _free_step(_sku(BIGQUERY_SERVICE, "993F-6B6B-DCC4", "Long Term Logical Storage",
                    "ApplicationServices", "LongTermStorage", 0, 10000000, "GiBy.mo",
                    ["us", "europe"])),
    _free_step(_sku(BIGQUERY_SERVICE, "0018-A5A0-9D6D", "Active Logical Storage (us-central1)",
                    "ApplicationServices", "ActiveStorage", 0, 23000000, "GiBy.mo",
                    ["us-central1"])),
    _free_step(_sku(BIGQUERY_SERVICE, "55B3-2F73-B6A3", "Long Term Logical Storage (us-central1)",
                    "ApplicationServices", "LongTermStorage", 0, 16000000, "GiBy.mo",
                    ["us-central1"])),
]}

STORAGE_SKUS = {"skus": [
    _sku(STORAGE_SERVICE, "NEARLINE-EUW1", "Nearline Storage", "Storage", "NearlineStorage",
         0, 10000000, "GiBy.mo", ["europe-west1"]),
    # The area's own record: `4CF0-069A-15D9`, `NearlineStorage`, 0.015 per GiB-month, offered at
    # `europe`. Neither `EU` nor `eu` appears in it, so the lookup is a table, not a lowercase fold.
    _sku(STORAGE_SERVICE, "4CF0-069A-15D9", "Nearline Storage Europe Multi-region", "Storage",
         "NearlineStorage", 0, 15000000, "GiBy.mo", ["europe"]),
]}

SERVICE_PAGE = {"services": [{"name": "compute.googleapis.com", "state": "ENABLED"},
                              {"name": "storage.googleapis.com", "state": "ENABLED"},
                              {"name": "bigquery.googleapis.com", "state": "ENABLED"},
                              {"name": "container.googleapis.com", "state": "ENABLED"},
                              {"name": "artifactregistry.googleapis.com", "state": "ENABLED"},
                              {"name": "androidmanagement.googleapis.com", "state": "DISABLED"}],
                "nextPageToken": "NEXT"}
SERVICE_PAGE_LAST = {"services": [{"name": "cloudbilling.googleapis.com", "state": "ENABLED"}]}

#: One unattached external IP (priced), one in use (UNPRICED), one internal (not a row).
ADDRESSES = [
    {"name": "ip-idle", "status": "RESERVED", "addressType": "EXTERNAL", "ipVersion": "IPV4",
     "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1",
     "creationTimestamp": "2025-03-04T10:00:00.000-08:00"},
    {"name": "ip-web", "status": "IN_USE", "addressType": "EXTERNAL",
     "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1",
     "users": ["https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/instances/web"],
     "creationTimestamp": "2025-03-04T10:00:00.000-08:00"},
    {"name": "ip-internal", "status": "RESERVED", "addressType": "INTERNAL",
     "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1"}]

ROUTES = {
    "cloudresourcemanager.projects.list": {
        "projects": [{"projectId": PROJECT, "name": f"projects/{PROJECT}", "state": "ACTIVE",
                      "displayName": "Seq Prod", "labels": {"team": "genomics"}},
                     {"projectId": OTHER, "name": f"projects/{OTHER}", "state": "ACTIVE",
                      "displayName": "Gen Dev", "labels": {"team": "imaging"}}]},
    "cloudresourcemanager.projects.search": {
        "projects": [{"projectId": PROJECT, "name": f"projects/{PROJECT}", "state": "ACTIVE",
                      "labels": {"team": "genomics"}},
                     {"projectId": OTHER, "name": f"projects/{OTHER}", "state": "ACTIVE",
                      "labels": {"team": "imaging"}},
                     {"projectId": "old-proj-1", "name": "projects/old-proj-1",
                      "state": "DELETE_REQUESTED", "labels": {"team": "genomics"}}]},
    # `projects.get` runs once per named project, so there is one page per project in command-line
    # order.
    "cloudresourcemanager.projects.get": [
        {"projectId": PROJECT, "name": f"projects/{PROJECT}", "state": "ACTIVE",
         "displayName": "Seq Prod", "labels": {"team": "genomics"}},
        {"projectId": OTHER, "name": f"projects/{OTHER}", "state": "ACTIVE",
         "displayName": "Gen Dev", "labels": {"team": "imaging"}}],
    # The scope read runs once per project and walks both pages, so two projects use four pages.
    # `fake_session` consumes a staged list in call order across the whole run.
    "serviceusage.services.list": [SERVICE_PAGE, SERVICE_PAGE_LAST,
                                   SERVICE_PAGE, SERVICE_PAGE_LAST],
    # Plain dicts, not lists: a dict route answers every call, and these reads run once per project.
    "compute.instances.aggregatedList": scoped(instances()),
    "compute.disks.aggregatedList": disk_scoped(disks()),
    "compute.machineTypes.aggregatedList": mt_scoped(machine_types()),
    # `images.list` and `snapshots.list` return rows under `items`, not under the resource name.
    "compute.addresses.aggregatedList": {"items": {"regions/us-central1": {"addresses": ADDRESSES}}},
    "compute.globalAddresses.list": {"items": []},
    "compute.images.list": {"items": images()},
    "compute.snapshots.list": {"items": snapshots()},
    "storage.buckets.list": BUCKETS,
    "monitoring.projects.timeSeries.list": GAUGE,
    # The gauge is silent about `lab-tmp`, so one `storage.objects.list` of one object runs. An
    # entry proves only "not empty", so the row stays `UNSIZED`. Nothing sums object sizes
    # (`docs/API-EVIDENCE.md` §`objects.list`).
    "storage.objects.list": {"items": [
        {"kind": "storage#object", "bucket": "lab-tmp", "id": "lab-tmp/tmp/moves.txt",
         "name": "tmp/moves.txt", "size": "4096", "updated": "2026-09-02T00:00:00Z"}]},
    "bigquery.datasets.list": DATASETS,
    "bigquery.tables.list": TABLES,
    ("bigquery.tables.get", "/tables/alignments"): TABLE_GET_ALIGNMENTS,
    ("bigquery.tables.get", "/tables/calls"): TABLE_GET_CALLS,
    # A `Service` names itself with `displayName`. `serviceId` is the bare id and `name` is
    # `services/<id>`; both are staged so that confusing them prices nothing.
    "cloudbilling.services.list": {"services": [
        {"serviceId": COMPUTE_SERVICE_ID, "name": COMPUTE_SERVICE,
         "displayName": "Compute Engine"},
        {"serviceId": STORAGE_SERVICE_ID, "name": STORAGE_SERVICE,
         "displayName": "Cloud Storage"},
        {"serviceId": BIGQUERY_SERVICE_ID, "name": BIGQUERY_SERVICE, "displayName": "BigQuery"},
        # Not BigQuery. Staged so a substring match on the sheet name would fail here.
        {"serviceId": "F33E-369C-A8B4", "name": "services/F33E-369C-A8B4",
         "displayName": "superQuery for BigQuery"}]},
    # One page per sheet, in the order the loader walks the three services it needs.
    "cloudbilling.services.skus.list": [COMPUTE_SKUS, STORAGE_SKUS, BIGQUERY_SKUS],
}


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = install(ROUTES)
    return tmp_path, fake


def run(staged, argv):
    out, fake = staged
    code = cli.main(argv + ["-o", str(out / "run")])
    return code, out / "run"


def read_csv(directory: Path) -> list[dict[str, str]]:
    with open(directory / "resources.csv", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------- the happy path

def test_a_run_writes_the_two_files_and_nothing_else(staged):
    code, out = run(staged, ["table", "--project", PROJECT])
    assert code == 0
    assert sorted(p.name for p in out.iterdir()) == ["README.txt", "resources.csv"]


def test_the_first_line_is_the_pinned_header_and_nothing_precedes_it(staged):
    run(staged, ["table", "--project", PROJECT])
    first = (staged[0] / "run" / "resources.csv").read_text(encoding="utf-8").splitlines()[0]
    assert first == model.CSV_HEADER


def test_every_resource_collected_becomes_a_row(staged):
    code, out = run(staged, ["table", "--project", PROJECT])
    rows = read_csv(out)
    keyed = {(r["kind"], r["item"]) for r in rows}
    assert ("Instance", "wdl-worker") in keyed
    assert ("Disk", "pd-worker") in keyed
    assert ("Disk", "pd-forgotten") in keyed
    assert ("Bucket", "lab-raw") in keyed
    assert ("Snapshot", "snap-2024-11") in keyed
    assert ("Image", "ubuntu-2004-lts-copy") in keyed
    assert code == 0


def test_the_table_keeps_a_cost_it_could_not_establish_and_says_why(staged):
    """An unpriced image stays in the table as `NA` with a reason. Dropping it would make a lower
    bound look like a total."""
    rows = read_csv(run(staged, ["table", "--project", PROJECT])[1])
    unpriced = [r for r in rows if r["usd_per_month"] == "NA"]
    assert unpriced, "every row priced: the fixture has resources this version cannot price"
    for row in unpriced:
        assert row["amount_state"] in {"UNPRICED", "UNSIZED", "REFUSED"}
        assert row["price_tier"] == "NA" and row["currency"] == "NA"
        assert "reason=" in row["evidence"]


def test_the_table_keeps_a_date_nobody_read(staged):
    rows = read_csv(run(staged, ["table", "--project", PROJECT])[1])
    undated = [r for r in rows if r["last_activity"] == "NA"]
    assert undated, "pd-forgotten has no attach record and must not inherit its creation date"
    assert all(r["last_activity_field"] == "not-read" for r in undated)
    # Only `pd-forgotten`, the disk with no attach record. Datasets have no timestamp, but
    # `collect_bigquery` does not emit dataset rows.
    assert {r["item"] for r in undated} == {"pd-forgotten"}


def test_a_priced_row_carries_a_number_a_tier_and_the_skus_own_date(staged):
    rows = {(r["kind"], r["item"]): r for r in read_csv(run(staged, ["table", "--project", PROJECT])[1])}
    disk = rows[("Disk", "pd-worker")]
    assert disk["amount_state"] == "MEASURED"
    assert float(disk["usd_per_month"]) > 0
    assert disk["price_tier"] == "CATALOG" and disk["currency"] == "USD"
    assert disk["price_as_of"] == "2026-09-01"
    instance = rows[("Instance", "wdl-worker")]
    assert instance["amount_state"] == "MEASURED"
    assert "cpus=4" in instance["evidence"]


def test_a_bucket_without_a_gauge_reading_is_unsized_and_not_zero(staged):
    """`lab-tmp` has no gauge series, so it is `UNSIZED`, not `0.00`. The follow-up object listing
    found an entry, so the bucket cannot be called empty either."""
    rows = {(r["kind"], r["item"]): r for r in read_csv(run(staged, ["table", "--project", PROJECT])[1])}
    bucket = rows[("Bucket", "lab-tmp")]
    assert bucket["amount_state"] == "UNSIZED"
    assert bucket["usd_per_month"] == "NA"
    assert "size_bytes" not in bucket["evidence"]


def test_a_multi_region_bucket_is_priced_on_the_area_record_it_owns(staged):
    """`lab-eu` is in `EU`, an area of many regions. It is priced on the area's own record,
    `4CF0-069A-15D9`, not on any single region's rate."""
    rows = {(r["kind"], r["item"]): r for r in read_csv(run(staged, ["table", "--project", PROJECT])[1])}
    bucket = rows[("Bucket", "lab-eu")]
    assert bucket["amount_state"] == "MEASURED"
    assert bucket["usd_per_month"] == "0.02"      # 1 GiB at 0.015, the pinned half-even `to_cell`
    assert bucket["price_tier"] == "CATALOG" and bucket["currency"] == "USD"
    assert "sku=4CF0-069A-15D9" in bucket["evidence"]
    assert "EU" in bucket["location"]             # the cell still prints what the cloud said


def test_a_snapshot_is_priced_at_the_place_its_storage_locations_named(staged):
    """`storageLocations` reaches the row and picks the record offered at that place. `location`
    stays `global`, as the API reports. Snapshot storage and bucket capacity are separate records,
    even in the same area."""
    rows = {(r["kind"], r["item"]): r for r in read_csv(run(staged, ["table", "--project", PROJECT])[1])}
    snap = rows[("Snapshot", "snap-2024-11")]
    assert snap["location"] == "global" and snap["amount_state"] == "MEASURED"
    assert snap["usd_per_month"] == "7.47"          # 114.91 GiB at 0.065 per GiB-month
    assert snap["price_tier"] == "CATALOG" and snap["currency"] == "USD"
    assert "storage_locations=us" in snap["evidence"]
    assert "sku=34CF-7D88-5D40" in snap["evidence"]
    assert "reason" not in snap["evidence"]


def test_the_readme_says_what_the_run_verified_and_what_it_did_not(staged):
    code, out = run(staged, ["table", "--project", PROJECT])
    readme = (out / "README.txt").read_text(encoding="utf-8")
    assert "layer1" in readme and "installed-by-test" in readme
    assert PROJECT in readme
    for column in model.CSV_HEADER.split(","):
        assert column in readme, f"{column} is in the header and not glossed in README.txt"
    assert "container.googleapis.com" in readme       # enabled, and stated as not enumerated
    assert "artifactregistry.googleapis.com" in readme


def test_the_readme_carries_no_total_and_no_advice(staged):
    run(staged, ["table", "--project", PROJECT])
    readme = (staged[0] / "run" / "README.txt").read_text(encoding="utf-8").lower()
    for banned in ("total", "sum(", "recommend", "you should", "delete "):
        assert banned not in readme, f"README.txt says '{banned}'"


def test_a_run_over_two_projects_marks_every_row_with_its_project(staged):
    code, out = run(staged, ["table", "--project", PROJECT, "--project", OTHER])
    rows = read_csv(out)
    assert code == 0
    assert {r["project"] for r in rows} == {PROJECT, OTHER}
    per_project = {p: sum(1 for r in rows if r["project"] == p) for p in (PROJECT, OTHER)}
    assert per_project[PROJECT] > 0 and per_project[OTHER] > 0, (
        "one project's rows standing in for the other's is how a merged estate reads as one project")


def test_a_failed_read_becomes_a_gap_and_the_rest_of_the_run_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    routes = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in ROUTES.items()}
    install(routes, failures={"compute.disks.aggregatedList": (403, "PERMISSION_DENIED")})
    code = cli.main(["table", "--project", PROJECT, "-o", str(tmp_path / "run")])
    assert code == 0
    rows = read_csv(tmp_path / "run")
    assert not any(r["kind"] == "Disk" for r in rows)
    assert any(r["kind"] == "Instance" for r in rows), "one refused read must not stop the pass"
    readme = (tmp_path / "run" / "README.txt").read_text(encoding="utf-8")
    assert "compute.disks.aggregatedList" in readme and "PERMISSION_DENIED" in readme


def test_a_refusal_raised_inside_a_collector_costs_one_read_not_the_whole_table(tmp_path, monkeypatch,
                                                                                capsys):
    """A `guard` or `methods` refusal raised inside one collector becomes a gap, not a fatal error.

    Cloud values such as a `tableId` go into URLs and can be refused. The other collectors' rows
    must still be written, and `README.txt` must name the refusal. An empty scope still exits 2; see
    `test_a_scope_that_resolves_to_nothing_refuses_instead_of_printing_an_empty_table`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    install(ROUTES)
    real = collect_bigquery_module.collect_bigquery

    def refusing(project, *, read_storage_info=False):
        if project == PROJECT:
            raise methods.MethodNotAllowed(
                "bigquery.tables.get: tableId='\u00e9v\u00e8nements' is not shaped like a tableId id")
        return real(project, read_storage_info=read_storage_info)

    monkeypatch.setattr(collect_bigquery_module, "collect_bigquery", refusing)
    code = cli.main(["table", "--project", PROJECT, "-o", str(tmp_path / "run")])

    assert code == 0, capsys.readouterr().err
    rows = read_csv(tmp_path / "run")
    assert any(r["kind"] == "Instance" for r in rows), "a raised refusal must not cost the other reads"
    readme = (tmp_path / "run" / "README.txt").read_text(encoding="utf-8")
    assert "MethodNotAllowed" in readme and "\u00e9v\u00e8nements" in readme
    assert "collector" in readme


def test_a_scope_that_resolves_to_nothing_refuses_instead_of_printing_an_empty_table(
        tmp_path, monkeypatch, capsys):
    """A `404` on `projects.get` leaves the scope empty. The run says so and writes nothing, since
    an empty table would read as "you have nothing"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = install({}, failures={"cloudresourcemanager.projects.get":
                                 (404, "NOT_FOUND Project not found")})
    code = cli.main(["table", "--project", "no-such-project", "-o", str(tmp_path / "run")])
    assert code == 2
    assert not (tmp_path / "run" / "resources.csv").exists()
    assert fake.count("cloudresourcemanager.projects.get") == 1
    assert "zero projects" in capsys.readouterr().err


def test_a_named_project_the_cloud_refused_to_answer_about_stops_the_run(tmp_path, monkeypatch,
                                                                        capsys):
    """A `403` on `projects.get` does not mean the project is empty.

    `projects.resolve_projects` drops the id. `cli.py` then refuses the empty scope with exit 2 and
    names the credential as the first thing to check.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = install({}, failures={"cloudresourcemanager.projects.get":
                                 (403, "PERMISSION_DENIED Required permission "
                                       "resourcemanager.projects.get")})
    code = cli.main(["table", "--project", PROJECT, "-o", str(tmp_path / "run")])
    assert code == 2
    assert not (tmp_path / "run").exists()
    assert fake.count("cloudresourcemanager.projects.get") == 1
    err = capsys.readouterr().err
    assert "zero projects" in err and "credential" in err
    assert f"{PROJECT}: " in err and "Required permission resourcemanager.projects.get" in err


@pytest.mark.parametrize("argv,expected", [
    (["--all"], {PROJECT, OTHER}),
    (["--label", "team=genomics"], {PROJECT}),
])
def test_all_and_a_bare_label_search_everything_the_credential_can_see(staged, argv, expected):
    """`projects.search`, then each row is re-checked for ACTIVE and the exact label. The
    DELETE_REQUESTED project has `team=genomics` and is still out of scope."""
    out, fake = staged
    code = cli.main(["table", *argv, "-o", str(out / "run")])
    assert code == 0
    assert {r["project"] for r in read_csv(out / "run")} == expected
    assert fake.count("cloudresourcemanager.projects.search") == 1
    assert fake.count("cloudresourcemanager.projects.list") == 0
    sent = fake.queries("cloudresourcemanager.projects.search")[0]
    assert sent.get("query") == ("labels.team:genomics" if "--label" in argv else None)


def test_an_unattached_ip_is_priced_and_an_attached_one_says_why_not(staged):
    out, _fake = staged
    assert cli.main(["table", "--project", PROJECT, "-o", str(out / "ip")]) == 0
    rows = {(r["kind"], r["item"]): r for r in read_csv(out / "ip")}

    idle = rows[("Address", "ip-idle")]
    assert idle["usd_per_month"] == "7.29" and idle["amount_state"] == "MEASURED"   # 729 h x $0.01
    assert "sku=66A2-68EA-56BE" in idle["evidence"]
    web = rows[("Address", "ip-web")]
    assert web["amount_state"] == "UNPRICED" and "in use" in web["evidence"]
    assert ("Address", "ip-internal") not in rows


def test_a_label_inside_a_folder_still_selects(staged):
    """`--label` filters the projects that `--folder` lists. Only `OTHER` has `team=imaging`."""
    out, fake = staged
    code = cli.main(["table", "--folder", "123456789012", "--label", "team=imaging",
                     "-o", str(out / "labelled")])
    assert code == 0
    assert {r["project"] for r in read_csv(out / "labelled")} == {OTHER}
    assert fake.queries("cloudresourcemanager.projects.list")[0]["parent"] == "folders/123456789012"
    assert fake.count("cloudresourcemanager.projects.get") == 0


def test_an_unresolvable_label_refuses_rather_than_returning_an_empty_list(staged, capsys):
    code, _ = run(staged, ["table", "--label", "Not A Label"])
    assert code == 2
    assert "refusing" in capsys.readouterr().err.lower()


def test_the_pass_makes_the_number_of_requests_the_fixture_stages(staged):
    """Bounds the request count for one project, so a runaway paging loop fails the test."""
    code, _ = run(staged, ["table", "--project", PROJECT])
    used = guard.stats()["requests"]
    assert 10 <= used <= 40, f"{used} requests for a one-project pass over eight reads"


# --------------------------------------------------------------------------- guard-check

# `TABLE_STORAGE` rows as a `QueryResponse`, columns in `collect_bigquery.TABLE_STORAGE_COLUMNS`
# order. One route answers both location queries; rows join on (dataset, table).
QUERY_ANSWER = {"jobComplete": True,
                "schema": {"fields": [{"name": name} for name in (
                    "table_schema", "table_name", "total_logical_bytes", "active_logical_bytes",
                    "long_term_logical_bytes", "storage_last_modified_time")]},
                "rows": [{"f": [{"v": v} for v in (
                             "variants", "calls", "536870912000", "429496729600", "107374182400",
                             "2026-10-01T04:00:00Z")]},
                         {"f": [{"v": v} for v in (
                             "eu_raw", "alignments", "32212254720", "21474836480", "10737418240",
                             "2026-10-02T04:00:00Z")]}]}


def test_the_storage_read_sizes_prices_and_dates_every_table(staged):
    """`--read-bq-storage` runs one query per location and no per-table reads. The view sizes,
    splits, prices and dates each row."""
    out, fake = staged
    fake.routes["bigquery.jobs.query"] = QUERY_ANSWER
    code = cli.main(["table", "--project", PROJECT, "--read-bq-storage", "-o",
                     str(out / "bq")])
    assert code == 0
    rows = {(r["kind"], r["item"]): r for r in read_csv(out / "bq")}

    calls = rows[("Table", "calls")]
    assert (calls["last_activity"], calls["last_activity_field"]) == (
        "2026-10-01", "TABLE_STORAGE.storage_last_modified_time")
    assert "size_source=TABLE_STORAGE" in calls["evidence"]
    assert "not_measured" not in calls["evidence"]
    assert calls["amount_state"] == "MEASURED"
    assert "sku=0018-A5A0-9D6D+55B3-2F73-B6A3" in calls["evidence"]   # the region's own pair

    area = rows[("Table", "alignments")]
    assert area["location"] == "EU"
    # (20 - 10) x 0.02 + (10 - 10) x 0.01, on the `[us, europe]` pair.
    assert area["usd_per_month"] == "0.20" and area["amount_state"] == "MEASURED"
    assert "sku=947D-3B46-7781+993F-6B6B-DCC4" in area["evidence"]
    assert area["last_activity"] == "2026-10-02"

    assert fake.count("bigquery.jobs.query") == 2
    assert fake.count("bigquery.tables.get") == 0
    sent = [call[3]["query"] for call in fake.calls if call[0] == "bigquery.jobs.query"]
    assert sorted(q.split("`")[3] for q in sent) == ["region-eu", "region-us-central1"]


def test_a_default_pass_sizes_every_table_by_metadata_without_buying_a_query(staged):
    out, fake = staged
    code = cli.main(["table", "--project", PROJECT, "-o", str(out / "default")])
    assert code == 0

    assert fake.count("bigquery.jobs.query") == 0
    assert fake.count("bigquery.tables.get") == 2

    rows = {(r["kind"], r["item"]): r for r in read_csv(out / "default")}
    area = rows[("Table", "alignments")]
    assert area["usd_per_month"] == "0.20" and area["amount_state"] == "MEASURED"
    assert "size_source=bigquery.tables.get/numBytes" in area["evidence"]
    assert "bytes=32212254720" in area["evidence"] and "gib=30.0" in area["evidence"]
    assert area["last_activity_field"] == "creationTime"
    assert "not_measured=TABLE_STORAGE not read (--read-bq-storage)" in area["evidence"]
    regional = rows[("Table", "calls")]
    assert regional["amount_state"] == "MEASURED"
    assert "size_source=bigquery.tables.get/numBytes" in regional["evidence"]

    readme = (out / "default" / "README.txt").read_text(encoding="utf-8")
    consent = [ln for ln in readme.splitlines() if ln.startswith("BigQuery reads:")]
    assert len(consent) == 1, consent
    assert "not authorised for this run" in consent[0]
    assert "bigquery.tables.get" in consent[0]


def test_guard_check_lists_the_policy_and_sends_nothing(staged, capsys):
    _out, fake = staged
    assert cli.main(["guard-check"]) == 0
    printed = capsys.readouterr().out
    assert "20 reads are allowlisted" in printed
    assert "bigquery.jobs.query" in printed and "--read-bq-storage" in printed
    assert "testIamPermissions" in printed        # listed as not checked
    assert fake.calls == []
    assert guard.stats()["requests"] == 0


def test_a_coverage_gap_names_its_service_and_a_read_failure_leads_the_console(capsys):
    """Each console gap line names its service, and read failures print before coverage gaps.

    A coverage gap's `read` is just `not-read`, so the service is the useful field. Failures come
    first because they explain an `NA`, and the console shows only the first few gaps.
    """
    standing = [model.Gap(project="lab-seq-prd", service=f"{name}.googleapis.com", read="not-read",
                          reason="enabled in this project and not enumerated by this version")
                for name in ("pubsub", "run", "container")]
    failed = [model.Gap(project="lab-seq-prd", service="storage.googleapis.com",
                        read="storage.buckets.list",
                        reason="HTTP 403 The caller does not have permission")]

    cli._print_gaps(standing + failed)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "gap:" in ln]
    assert len(lines) == 4
    assert "storage.googleapis.com storage.buckets.list" in lines[0]
    assert [gap.split()[2] for gap in lines[1:4]] == [
        "container.googleapis.com", "pubsub.googleapis.com", "run.googleapis.com"], lines

    many = [model.Gap(project=f"lab-prj-{n:02d}", service="pubsub.googleapis.com", read="not-read",
                      reason="enabled in this project and not enumerated by this version")
            for n in range(13)] + failed
    cli._print_gaps(many)
    out = capsys.readouterr().out
    tail = [ln for ln in out.splitlines() if "more" in ln]
    assert len(tail) == 1 and "13 of 14 are services this version does not enumerate" in tail[0]
    assert "gap: lab-prj-00" not in out.splitlines()[0]
    assert "storage.googleapis.com storage.buckets.list" in out.splitlines()[0]


def test_a_typed_project_the_cloud_refused_is_named_in_the_report(staged, capsys):
    """Of two named projects, the one `projects.get` refused appears in the report as a gap.

    A partly resolved scope is a gap that names the project. It is neither clean nor empty.
    """
    tmp, fake = staged
    fake.failures[("cloudresourcemanager.projects.get", OTHER)] = (
        403, "PERMISSION_DENIED Required permission resourcemanager.projects.get")

    code = cli.main(["table", "--project", PROJECT, "--project", OTHER, "-o", str(tmp / "run")])
    assert code == 0, "one resolved project is still a table; only a whole failed scope refuses"

    readme = (tmp / "run" / "README.txt").read_text()
    covered = [ln for ln in readme.splitlines() if ln.startswith("projects covered:")][0]
    assert PROJECT in covered and OTHER not in covered

    lines = [ln for ln in readme.splitlines() if OTHER in ln and "projects.get" in ln]
    assert len(lines) == 1, lines
    assert "not about the project being empty" in lines[0]
    assert not [row for row in read_csv(tmp / "run") if row["project"] == OTHER]
    assert OTHER in capsys.readouterr().out


# ------------------------------------------------- TERMINATED VMs: the default, and its opt-in

def instance(name: str, status: str) -> dict:
    """One VM, on the machine type this fixture prices, in the one zone it stages."""
    return {"id": name, "name": name, "machineType": MACHINE_TYPE,
            "zone": f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"
                    "/zones/us-central1-a",
            "status": status,
            "creationTimestamp": "2025-01-05T04:00:00.000-08:00",
            "lastStartTimestamp": "2026-09-27T03:11:00.000-07:00"}


def stage_instances(fake, *items):
    """Re-stage the instances read for one test. The route is a plain dict, so one payload answers
    every project's call."""
    fake.routes["compute.instances.aggregatedList"] = scoped(list(items))


def terminated_line(directory: Path) -> str:
    lines = [ln for ln in (directory / "README.txt").read_text(encoding="utf-8").splitlines()
             if ln.startswith("Instance rows reporting TERMINATED:")]
    assert len(lines) == 1, lines
    return lines[0]


def test_a_terminated_instance_is_left_out_and_the_run_says_so_in_both_places(staged, capsys):
    """A TERMINATED VM is left out by default, and both `README.txt` and the console say so.

    CPU bills in `RUNNING` and `PENDING_STOP`, and memory in those plus `SUSPENDING`/`SUSPENDED`
    (`docs/API-EVIDENCE.md` §`Instance.status`). A stopped VM's row would show a cost nobody pays.
    Its disks still bill and keep their rows.
    """
    tmp, fake = staged
    stage_instances(fake, instance("wdl-worker", "RUNNING"),
                    instance("vm-stopped-1", "TERMINATED"))

    assert cli.main(["table", "--project", PROJECT, "-o", str(tmp / "run")]) == 0

    rows = read_csv(tmp / "run")
    assert {r["item"] for r in rows if r["kind"] == "Instance"} == {"wdl-worker"}
    assert {r["item"] for r in rows if r["kind"] == "Disk"} == {"pd-worker", "pd-forgotten"}, (
        "the withheld VM's disks are billed whatever its state and keep their own rows")

    line = terminated_line(tmp / "run")
    assert "1 of 2 left out of resources.csv by default" in line
    assert "keep their own rows" in line and "--include-terminated" in line

    printed = capsys.readouterr().out
    assert "1 TERMINATED Instance row(s) are not in resources.csv" in printed
    assert "--include-terminated" in printed


def test_the_flag_is_the_only_difference_between_the_two_tables(tmp_path, monkeypatch):
    """`--include-terminated` adds exactly the rows the default run reported leaving out.

    Comparing both runs separates "left out by default" from "never collected". The counts in
    `README.txt` make that difference visible.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    routes = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in ROUTES.items()}
    routes["compute.instances.aggregatedList"] = scoped(
        [instance("wdl-worker", "RUNNING"), instance("vm-stopped-1", "TERMINATED"),
         instance("vm-custom-ext", "TERMINATED")])

    def pass_to(directory: Path, *argv) -> set[tuple[str, str]]:
        install(routes)                       # a clean guard, so the second pass re-reads the sheets
        assert cli.main(["table", "--project", PROJECT, "-o", str(directory), *argv]) == 0
        return {(r["kind"], r["item"]) for r in read_csv(directory)}

    default = pass_to(tmp_path / "default")
    flagged = pass_to(tmp_path / "flagged", "--include-terminated")

    assert flagged - default == {("Instance", "vm-stopped-1"), ("Instance", "vm-custom-ext")}
    assert not default - flagged, "the default table must be the flagged one minus the rows it named"
    assert terminated_line(tmp_path / "default").startswith(
        "Instance rows reporting TERMINATED: 2 of 3 left out")
    assert "2 of 3 listed, because --include-terminated was given" in terminated_line(
        tmp_path / "flagged")
    assert "TERMINATED instances: listed" in (tmp_path / "flagged" /
                                              "README.txt").read_text(encoding="utf-8")


def test_the_exclusion_is_one_named_pair_and_not_a_rule_about_being_switched_on(staged):
    """Only the `(Instance, TERMINATED)` pair is left out. Every other state stays.

    A `SUSPENDED` VM still bills memory. `STOPPED` is outside the set because it has not been
    observed; widening `cli.OMITTED_BY_DEFAULT` needs a maintainer's decision. The `TERMINATED` disk
    shows that the state alone decides nothing.
    """
    tmp, fake = staged
    stage_instances(fake, instance("on-now", "RUNNING"), instance("off-legacy", "STOPPED"),
                    instance("asleep", "SUSPENDED"), instance("asleep-still", "SUSPENDING"),
                    instance("booting", "STAGING"), instance("gone", "TERMINATED"))
    routes = fake.routes
    routes["compute.disks.aggregatedList"] = disk_scoped(
        disks() + [{"id": "9", "name": "pd-weird-state", "sizeGb": "10", "status": "TERMINATED",
                    "zone": "us-central1-a", "type": "pd-ssd"}])

    assert cli.main(["table", "--project", PROJECT, "-o", str(tmp / "run")]) == 0

    rows = read_csv(tmp / "run")
    assert {r["item"] for r in rows if r["kind"] == "Instance"} == {
        "on-now", "off-legacy", "asleep", "asleep-still", "booting"}
    assert "pd-weird-state" in {r["item"] for r in rows if r["kind"] == "Disk"}, (
        "the pair is (Instance, TERMINATED); a disk in that spelling is still billed and still a row")
    assert "1 of 6 left out of resources.csv by default" in terminated_line(tmp / "run")


def test_a_refused_instances_read_does_not_get_to_say_nothing_is_terminated(tmp_path, monkeypatch):
    """When the instances read fails, the `0 of 0` line must say the read did not answer.

    Otherwise the line reads as a clean result. The gap section names the failed read.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    routes = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in ROUTES.items()}
    routes["compute.instances.aggregatedList"] = scoped([instance("wdl-worker", "RUNNING")])
    install(routes, failures={"compute.instances.aggregatedList":
                             (403, "PERMISSION_DENIED compute.instances.list")})

    assert cli.main(["table", "--project", PROJECT, "-o", str(tmp_path / "run")]) == 0

    assert not [r for r in read_csv(tmp_path / "run") if r["kind"] == "Instance"]
    line = terminated_line(tmp_path / "run")
    assert "0 of 0" in line
    assert "The instances read did not answer for all of this scope" in line
    readme = (tmp_path / "run" / "README.txt").read_text(encoding="utf-8")
    assert "PERMISSION_DENIED compute.instances.list" in readme
