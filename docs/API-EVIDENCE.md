# API evidence

This page covers each request in [`src/gcp_audit/methods.py`](../src/gcp_audit/methods.py): why it
only reads, which scope it uses, and the non-obvious facts about Google's APIs that the code relies
on. Method ids, paths and scopes come from Google's discovery documents
(`https://<api>/$discovery/rest?version=<v>`) at the revisions listed below. Anything marked
*observed* is what a live server returned, not what a document says.

Full response schemas are not copied here; see the discovery document or the API reference.

## The five scopes a run may hold

`guard.NARROW_SCOPES`, without the `https://www.googleapis.com/auth/` prefix:

| scope | declared by (revision) | reads that use it |
|---|---|---|
| `cloud-platform.read-only` | Resource Manager v3 (20260917), Service Usage v1 (20260921), Storage v1 (20260922), BigQuery v2 (20260811) | `cloudresourcemanager.*`, `serviceusage.services.list`, `storage.*`, `bigquery.*` |
| `devstorage.read_only` | Storage v1 (20260922) | `storage.buckets.list`, `storage.objects.list` |
| `compute.readonly` | Compute v1 (20260922) | every `compute.*` read |
| `monitoring.read` | Monitoring v3 (20260917) | `monitoring.projects.timeSeries.list` |
| `cloud-billing.readonly` | Cloud Billing v1 (20260911) | `cloudbilling.services.list`, `cloudbilling.services.skus.list` |

* **BigQuery has no read-only scope of its own.** BigQuery v2 declares `bigquery`,
  `bigquery.insertdata`, `cloud-platform`, `cloud-platform.read-only` and three `devstorage.*`
  scopes, but no `bigquery.readonly`. Its read methods accept `bigquery`, `cloud-platform` and
  `cloud-platform.read-only`, so `gca` uses `cloud-platform.read-only`.
* **Cloud Billing accepts `cloud-billing.readonly`** but not `cloud-platform.read-only`.
* Storage reads also accept `devstorage.read_write`, but that scope can write objects, so `gca`
  never accepts it.
* Every request on the allowlist accepts at least one of the five scopes. `tests/test_guard.py`
  checks this against the allowlist.

## The reads

| method id | path | fields the code uses |
|---|---|---|
| `cloudresourcemanager.projects.list` | `GET v3/projects?parent=folders/<id>` | `projectId`, `state`, `labels` |
| `cloudresourcemanager.projects.get` | `GET v3/projects/<id>` | `projectId`, `state`, `labels` |
| `cloudresourcemanager.projects.search` | `GET v3/projects:search` | as above |
| `serviceusage.services.list` | `GET v1/projects/<id>/services` | `name`, `state`, `config` |
| `compute.instances.aggregatedList` | `GET compute/v1/projects/<id>/aggregated/instances` | `name`, `zone`, `status`, `machineType`, `creationTimestamp`, `lastStartTimestamp`, `disks[].type`, `guestAccelerators[]`, `scheduling.provisioningModel`, `scheduling.preemptible` |
| `compute.disks.aggregatedList` | `… /aggregated/disks` | `name`, `zone`/`region`, `type`, `sizeGb`, `status`, `users[]`, `lastAttachTimestamp` |
| `compute.images.list` | `… /global/images` | `name`, `status`, `creationTimestamp` |
| `compute.snapshots.list` | `… /global/snapshots` | `name`, `storageBytes`, `storageBytesStatus`, `storageLocations`, `creationTimestamp` |
| `compute.machineTypes.aggregatedList` | `… /aggregated/machineTypes` | `name`, `guestCpus`, `memoryMb`, `isSharedCpu`, `accelerators[]` |
| `compute.addresses.aggregatedList` | `… /aggregated/addresses` | `name`, `region`, `status`, `addressType`, `users[]`, `networkTier`, `ipVersion`, `creationTimestamp` |
| `compute.globalAddresses.list` | `… /global/addresses` | as above |
| `storage.buckets.list` | `GET storage/v1/b?project=<id>` | `name`, `location`, `storageClass`, `updated`, `versioning.enabled` |
| `storage.objects.list` | `GET storage/v1/b/<bucket>/o?maxResults=1` | `items`, `prefixes`, `nextPageToken` (presence only) |
| `monitoring.projects.timeSeries.list` | `GET v3/projects/<id>/timeSeries` | `timeSeries[].points[].value`, `resource.labels.bucket_name` |
| `bigquery.datasets.list` | `GET bigquery/v2/projects/<id>/datasets` | `datasetReference`, `location` |
| `bigquery.tables.list` | `… /datasets/<ds>/tables` | `tableReference`, `type`, `creationTime` |
| `bigquery.tables.get` | `… /datasets/<ds>/tables/<t>` | `tableReference`, `numBytes`, `numActiveLogicalBytes`, `numLongTermLogicalBytes` |
| `bigquery.jobs.query` | `POST bigquery/v2/projects/<id>/queries` | `jobComplete`, `schema`, `rows`, `pageToken` |
| `cloudbilling.services.list` | `GET v1/services` | `name`, `serviceId`, `displayName` |
| `cloudbilling.services.skus.list` | `GET v1/services/<id>/skus` | `skuId`, `description`, `category`, `serviceRegions`, `geoTaxonomy`, `pricingInfo[]` |

`bigquery.jobs.query` is the only POST, and it runs only with `--read-bq-storage`. Its query is
shown under [`INFORMATION_SCHEMA.TABLE_STORAGE`](#information_schematable_storage).

## Resource Manager

**`projects.list` requires a parent** (*observed* 2026-10-20). The discovery document does not mark
any parameter as required, but the description of `parent` begins "Required.", and a request
without one returned `HTTP 400 INVALID_ARGUMENT field [parent] has issue [invalid parent name]`.
So:

* a project named with `--project` is checked with `projects.get`, one request per id;
* `--folder` uses `projects.list?parent=folders/<id>`;
* `--all`, and `--label` without `--folder`, use `projects.search`.

**`projects.search` matches any of several conditions, not all of them.** The `query` parameter's
description says: "If multiple fields are included in the query, then it will return results that
match any of the fields," and "Search expressions are case insensitive." The method returns the
projects on which the caller has `resourcemanager.projects.get`, and it is "eventually consistent
with project mutations". So `projects.py` sends at most one `labels.<key>:<value>` term (or
`labels.<key>:*`), then checks `state == ACTIVE` and the exact label on every result.

## Compute Engine

### `Instance.status`

`Instance.status` has twelve values: `DEPROVISIONING`, `PENDING`, `PENDING_STOP`, `PROVISIONING`,
`REPAIRING`, `RUNNING`, `STAGING`, `STOPPED`, `STOPPING`, `SUSPENDED`, `SUSPENDING`, `TERMINATED`.

Which states are billed comes from the [instance lifecycle
page](https://cloud.google.com/compute/docs/instances/instance-lifecycle) (read 2026-10-23):

> * For CPU usage, you incur charges when the compute instance is in the following states:
>   `RUNNING`, `PENDING_STOP`
> * For memory usage, you incur charges when the compute instance is in the following states:
>   `RUNNING`, `PENDING_STOP`, `SUSPENDING`, `SUSPENDED`
> * For attached resources like disks or external IP addresses, you incur charges while the
>   resources exist, regardless of the compute instance state.

This is why `TERMINATED` VMs are left out by default while their disks keep their rows. `STOPPED`
and `SUSPENDED` VMs get no special treatment, because no run has yet seen one to check against a
bill.

### Fields that change what a VM bills

| field | values | effect |
|---|---|---|
| `Instance.guestAccelerators[]` (`acceleratorType`, `acceleratorCount`) | | attached GPUs, priced at their own rate |
| `MachineType.accelerators[]` (`guestAcceleratorType`, `guestAcceleratorCount`) | | GPUs built into the machine type (A2) |
| `Scheduling.provisioningModel` | `STANDARD`, `SPOT`, `FLEX_START`, `RESERVATION_BOUND` | `SPOT` uses Spot rates; the last two are `UNPRICED` |
| `Scheduling.preemptible` | boolean | older flag; priced as Spot |
| `AttachedDisk.type` | `PERSISTENT`, `SCRATCH` | `SCRATCH` is a local SSD: `UNPRICED` |
| `MachineType.isSharedCpu` | boolean | shared-core: `UNPRICED` |

### Addresses

`Address.status` is `IN_USE`, `RESERVED` or `RESERVING`. `addressType` is `EXTERNAL`, `INTERNAL`
or `UNSPECIFIED_TYPE` ("If unspecified, defaults to EXTERNAL"). Only external addresses get rows,
and only `RESERVED` IPv4 addresses are priced.

### Snapshots and images

Snapshots are billed on `storageBytes`. A snapshot's `diskSizeGb` is the size of the source disk,
which overstates it. `storageBytesStatus = LIKELY_INCOMPLETE` means the size is a lower bound.

`Image` has no `storageBytes`. Its size fields are `archiveSizeBytes` ("Size of the image tar.gz
archive stored in Google Cloud Storage") and `diskSizeGb` ("Size of the image when restored onto a
persistent disk"). Neither is the storage being billed, so images are listed but left `UNSIZED`.

A snapshot's rate depends on `storageLocations`, not on its location (`global`). The lab's
snapshots had `["us"]` (*observed* 2026-10-13) and are priced on `34CF-7D88-5D40` "Storage PD
Snapshot in US" (`PDSnapshot`, 0.065 USD per `GiBy.mo`, offered at `us`).

## Cloud Storage

`buckets.list` returns no size, so the size comes from the Cloud Monitoring metric
`storage.googleapis.com/storage/total_bytes`. A metric with no data points does not mean zero
bytes.

### `objects.list`

Used only for a bucket with no size data and with versioning off: `gca` asks for one page with
`maxResults=1`. A page with no `items`, no `prefixes` and no `nextPageToken` counts as a measured
zero. Object sizes are never added up.

## BigQuery

### `tables.get`

* `numBytes`: "The size of this table in logical bytes, excluding any data in the streaming
  buffer." So a table that is receiving streamed data is undercounted.
* `numActiveLogicalBytes` / `numLongTermLogicalBytes`: logical bytes less than / more than 90 days
  old. These match the two storage price records under §The BigQuery sheet.
* The `view` parameter is not allowed. Without it, the response includes storage statistics.
  With `view=BASIC`, it "does not include storage statistics such as numRows or numBytes", which
  would silently empty the size column.
* `numPhysicalBytes` is returned but not used, because physical-storage billing is not modelled.

### `INFORMATION_SCHEMA.TABLE_STORAGE`

The query that `jobs.query` sends (`collect_bigquery.TABLE_STORAGE_SQL`):

```sql
SELECT table_schema, table_name, total_logical_bytes, active_logical_bytes,
       long_term_logical_bytes, storage_last_modified_time
FROM `<project>`.`region-<location>`.INFORMATION_SCHEMA.TABLE_STORAGE
WHERE deleted IS NOT TRUE
```

From the [view's documentation](https://cloud.google.com/bigquery/docs/information-schema-table-storage)
(read 2026-10-27): "Queries against this view must include a region qualifier", written
`` `region-REGION` ``, where `REGION` is "any dataset region name", including multi-regions. All the
columns above are documented; `storage_last_modified_time` is "The most recent time that data was
written to the table". The view has no last-read column.

Versions before 2.0.0 left out the `region-` prefix and asked for columns that do not exist. A live
run on 2026-10-21 failed with `NOT_FOUND … Dataset <project>:US.INFORMATION_SCHEMA was not found`.
The corrected query has not yet run live (`DESIGN.md` §Open questions 1).

## Cloud Billing Catalog

**`name` and `serviceId` differ.** `Service.name` is `services/6F81-5844-456A`, while
`Service.serviceId` is just `6F81-5844-456A`. `services.skus.list` takes `parent=services/<id>`, so
`gca` requests a price list by `name`. Mixing the two up once made every price fail. The price
lists used:

| `displayName` | service id |
|---|---|
| `Compute Engine` | `6F81-5844-456A` |
| `Cloud Storage` | `95FF-2EF5-5EA1` |
| `BigQuery` | `24E6-581D-38E5` |

Six other services contain "BigQuery" in their name (Reservation API, BI Engine, Data Transfer,
Storage API, and two others), so `gca` matches the exact `displayName`.

**`effectiveTime` dates the price list, not the price.** In four downloads (2026-10-09 to
2026-10-19, about 35,000 records each), every record but one had the same timestamp: the most
recent 07:00 UTC before the download. So `price_as_of` shows how fresh the price list is, and a run
before 07:00 UTC shows the previous day.

### Twelve Cloud Storage capacity records offered at an area

Multi-region buckets (`US`, `EU`, `ASIA`) have their own price records, listed under the lowercase
keys `us`, `europe` and `asia` (downloaded 2026-10-19; per `GiBy.mo`, one tier each):

| class | `resourceGroup` | US | Europe | Asia |
|---|---|---|---|---|
| Standard | `MultiRegionalStorage` | `0D5D-6E23-4250` 0.026 | `EC40-8747-D6FF` 0.026 | `E653-0A40-3B69` 0.026 |
| Nearline | `NearlineStorage` | `9E8A-BB82-26BF` 0.015 | `4CF0-069A-15D9` 0.015 | `DEEA-BC30-584C` 0.015 |
| Coldline | `ColdlineStorage` | `37C4-203D-1024` 0.007 | `6CCC-4CDD-383C` 0.007 | `2D3D-C91F-7E7C` 0.00875 |
| Archive | `ArchiveStorage` | `1EA2-2E06-552E` 0.0024 | `5A75-E003-2CBF` 0.0024 | `4B5C-6444-A206` 0.003 |

* These differ from single-region rates. `us` Standard is 0.026, while `us-central1` Standard is
  0.02 after a 5 GiB free tier and sits in a different group (`RegionalStorage`).
  `pricing.AREA_PLACEMENTS` and `pricing.AREA_GROUPS` map each area to its key and group.
* The key `eu` exists, but only for Autoclass records (for example `83B3-F178-99CB`, group
  `Storage`). Europe's regular records are under `europe`. Matching on the start of the description
  and on the group keeps the two apart.
* The same groups also contain `(Early Delete)` records under these keys, priced per `GiBy.d`;
  the unit filter excludes them.

### The BigQuery sheet

Table storage is billed on two records (downloaded 2026-10-22):

| `skuId` | description | `resourceGroup` | offered at | USD/GiB-month |
|---|---|---|---|---|
| `947D-3B46-7781` | Active Logical Storage | `ActiveStorage` | `us`, `europe` | first 10 free, then 0.02 |
| `993F-6B6B-DCC4` | Long Term Logical Storage | `LongTermStorage` | `us`, `europe` | first 10 free, then 0.01 |

* Single regions have their own pairs (for `us-central1`, `0018-A5A0-9D6D` at 0.023 and
  `55B3-2F73-B6A3` at 0.016). There is no logical-storage record for `asia`, so an `ASIA` dataset
  is `UNPRICED`.
* The same price list also has physical-storage records (`Active Physical Storage (US)` and
  others), plus `Analysis` and `Streaming Insert` under the same `[us, europe]` keys. A record must
  match on both the start of its description and its `resourceGroup`.

### Compute Engine records for Spot VMs, GPUs and static IPs

Taken from Google's public SKU pages (read 2026-10-27) and, where marked *, from a 2026-09-29
download of the Compute Engine price list. Each description continues with ` running in <place>`
or ` in <place>`.

| use | description begins | example SKU |
|---|---|---|
| Spot E2 | `Spot Preemptible E2 Instance Core` / `… Ram` | `6973-FC9A-3477`, `64F3-9E6F-1C3E` |
| Spot N1 | `Spot Preemptible N1 Predefined Instance Core` / `… Ram`; `Spot Preemptible Custom Instance Core` / `… Ram`; `Spot Preemptible Custom Extended Instance Ram` | `D498-1ECA-87C1`*, `5451-0A15-0123`* |
| Spot N2 | `Spot Preemptible N2 Instance Core` / `… Ram`; `… N2 Custom Instance Core` / `… Ram`; `… N2 Custom Extended Instance Ram` | `A005-73BB-9999`, `741E-C653-D52D` |
| Spot N2D | `Spot Preemptible N2D AMD Instance Core` / `… Ram`; `… N2D AMD Custom Instance Core` / `… Ram`; `… N2D AMD Custom Extended Instance Ram` | `6B4A-3961-E53F`, `672C-2EB5-278B` |
| GPU | `Nvidia Tesla T4 GPU`, `… P4 GPU`, `… P100 GPU`, `… V100 GPU`, `… A100 GPU`, `… A100 80GB GPU` | `88B8-C3ED-03F0` (T4) |
| GPU on Spot | `Nvidia Tesla T4 GPU attached to Spot Preemptible VMs` (only T4 confirmed) | `1A25-07A3-AB6D` |
| unattached IP | `Static Ip Charge` (Network / IpAddress, hourly; first hour free, then 0.01)* | `66A2-68EA-56BE`* |

## APIs not read

| API | why |
|---|---|
| GKE (`container` v1) | its only read-only scope, `container.read-only`, has not been confirmed to work for a service account |
| Cloud Asset (`cloudasset` v1p1beta1) | `resources.searchAll` accepts only `cloud-platform`, which layer 1 refuses |
| Cloud Run, Cloud Functions, Pub/Sub, Dataproc, Artifact Registry, Cloud SQL, Spanner | no read-only scope of their own, or none confirmed to work for a service account |
| Monitoring `metricDescriptors.list` | not needed, because the metric read names its metric in `filter` |
