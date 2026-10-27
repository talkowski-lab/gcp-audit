# Design

`gca` reads a set of projects and writes `resources.csv`, with one row for each resource Google
bills for. A row gives what the resource is, its monthly cost at list price, and the last date any
API reports it was used. This page explains what each cell means, which API read produced it, and
what is deliberately left out. Where this page and [`SAFETY.md`](SAFETY.md) disagree, SAFETY wins.

## Overview

```
projects (--project / --folder / --label / --all)         projects.py
  └─ collect   a few GETs per service per project         collect_compute / _storage / _bigquery
      └─ price dated Billing Catalog SKUs                 pricing.py
          └─ table   resources.csv + README.txt           table.py
```

Everything after `collect` runs offline and is deterministic: the same API responses always produce
the same files. `guard.py` is the only code that touches the network.

## The columns

`model.CSV_HEADER` is pinned byte for byte, and **each column holds one fact**.

| column | holds | `NA` when |
|---|---|---|
| `project`, `service`, `kind`, `item` | project id, API domain, resource kind (`Instance`, `Disk`, `Image`, `Snapshot`, `Address`, `Bucket` or `Table`), and the resource's name | never |
| `location` | zone, region, multi-region or `global`, as the API spells it | the API gave no location |
| `state` | the API's status, such as `RUNNING` | the API has no status field |
| `usd_per_month` | a number and nothing else | there is no trustworthy number |
| `price_tier` | `CATALOG`: measured quantity × dated list price | no price was used |
| `price_as_of` | the price record's `effectiveTime`. Google re-stamps every record daily, so this dates the price list, not the price | no price was used |
| `currency` | the price's currency | no price was used |
| `amount_state` | `MEASURED`, `UNSIZED`, `UNPRICED` or `REFUSED` | never |
| `last_activity` | `YYYY-MM-DD` | no API gave a date |
| `last_activity_field` | the API field the date came from | never (`not-read` instead) |
| `evidence` | `key=value` pairs separated by `;`, keys sorted, one per observed fact | never (`no-evidence-recorded` instead) |

Rules behind these columns:

* **Use `NA`, never `0.00`, a blank or a dash.** Spreadsheets read a blank as zero, and people read
  zero as free.
* **The cost cell is a bare number, with currency in its own column**, so `SUM()` works.
* **`amount_state` says why a cost is `NA`.** `UNSIZED`: nothing gave a size. `UNPRICED`: there is a
  size but no applicable rate. `REFUSED`: a read that would have answered failed. `evidence` gives
  the details.
* **A fact nobody read is left out of `evidence`, never recorded as `0`.** `0` means an API said
  zero.
* **There is no totals row.** Rows mix states and currencies, and some free tiers apply once per
  billing account (open question 6).
* **A row that cannot be priced stays in the table.** Dropping it would make a partial list look
  like a complete total.

## Where "last used" comes from

No API reports "last used" for every kind of resource, and the fields that exist mean different
things. So the table prints the date **and** the field it came from.

| kind | date field | what it actually means |
|---|---|---|
| Instance | `lastStartTimestamp`, else `creationTimestamp` | last boot. A VM that has run for months without a restart looks old |
| Disk | `lastAttachTimestamp` | last attach. A disk that was never attached has no date |
| Bucket | `updated` | last **metadata** change, not last data access. Editing a lifecycle rule updates it |
| Image, Snapshot, Address | `creationTimestamp` | creation only |
| Table | `storage_last_modified_time` from `TABLE_STORAGE` (with `--read-bq-storage`), else `creationTime` | last data **write**. BigQuery does not record when a table was last read |

Cloud Audit Logs (Data Access) could say who read what, but `gca` does not read them. They need
`logs.viewer`, they are slow to query, and several services leave them off by default, so a missing
entry would mean "unknown", not "unused".

## Sizing and money

Cost is measured quantity × one dated list price from the Billing Catalog. It is a run-rate
estimate, not an invoice. Committed-use and sustained-use discounts are not modelled, because
guessing a discount without the invoice would invent a number.

* **Storage is billed per GiB (2³⁰ bytes).** There is deliberately no decimal-GB helper, because
  applying a per-GiB rate to `bytes / 1e9` overcharges by 7.37%. Persistent disks are the
  exception: `disks.sizeGb` is already in the billing unit.
* **VMs** cost `vCPUs × core rate + memory × GiB rate`. Both rates are hourly, and a month is
  `model.HOURS_PER_MONTH = 730` hours (Google's convention, stated in `README.txt`).
  * vCPU and memory counts come from `machineTypes.aggregatedList`. Custom machine types are not in
    that list, so their counts are parsed from the name (for example, `n2d-custom-2-24832-ext`).
  * Custom types are priced on their family's **Custom** records. With `-ext`, memory above the
    family's per-vCPU limit (`pricing.RAM_CEILINGS`, taken from Google's machine-family page) is
    priced on the extended-memory record.
  * Only the `e2`, `n1`, `n2` and `n2d` families have core and memory records mapped; other families
    are `UNPRICED`.
  * **Spot and preemptible VMs** (`scheduling.provisioningModel = SPOT`, or the older
    `preemptible: true`) are priced on the `Spot Preemptible …` records (`pricing.SPOT_FAMILIES`).
    Spot prices change often; the row carries the record's date like any other.
  * **GPUs** (`guestAccelerators`) add `count × GPU rate × 730`. On-demand records are named
    `Nvidia Tesla <model> GPU running in …`, and Spot ones `… GPU attached to Spot Preemptible VMs
    running in …` (`pricing.GPU_MODELS`). A VM with more than one GPU model is refused.
  * **These stay `UNPRICED`, with the reason recorded:** local SSDs, shared-core machine types, and
    `FLEX_START` or `RESERVATION_BOUND` provisioning. No price is better than a partial one.
* **Disks** are priced by type (`pd-standard`, `pd-ssd`, and so on, read from the type URL). A disk
  whose type was not read is `UNPRICED`, not priced as the most common type. Regional disks use
  their own regional records.
* **External IP addresses** get rows; internal ones are free and get none. A `RESERVED`
  (unattached) IPv4 address is priced hourly on `Static Ip Charge`, whose first hour is free. An
  `IN_USE` address is `UNPRICED`, because it is billed as part of the VM or load balancer using it,
  which is not modelled. A global address has no regional record and is `UNPRICED`.
* **Snapshots** are priced on `storageBytes`, never `diskSizeGb` (the size of the source disk). `storageBytesStatus = LIKELY_INCOMPLETE` means the size is a lower bound. A snapshot's
  rate depends on `storageLocations`: exactly one location is priced; none, or several, is
  `UNPRICED`.
* **Images** are listed but `UNSIZED`: the API reports only the archive size and the restored disk
  size, neither of which is the storage billed.
* **Buckets** are sized by the Cloud Monitoring `storage/total_bytes` metric over the last 7 days,
  because `buckets.list` reports no size.
  * A bucket with no data points is `UNSIZED`, never zero.
  * For such a bucket, if versioning is off, `gca` lists one object (`storage.objects.list`,
    `maxResults=1`). An empty result is a measured `0`. Object sizes are never added up from a
    listing.
  * Objects that are retained, held or soft-deleted are still billed but do not appear in that
    listing. `evidence` names the read that produced a zero.
* **BigQuery tables** are priced on two records, one for active and one for long-term logical
  bytes. The split comes from `tables.get` (`numActiveLogicalBytes` and `numLongTermLogicalBytes`)
  or from `TABLE_STORAGE` (`active_logical_bytes` and `long_term_logical_bytes`). The split is used
  only when the two halves add up to the total; otherwise the row is `UNPRICED` and quotes the
  numbers. A table billed on physical storage is still priced on logical bytes (open question 7).
* **Choosing a price record:**
  * A record is matched by the start of its description, never by a word inside it. The same group
    contains `Autoclass Standard Storage …` next to `Standard Storage …`, and `Spot Preemptible …`
    next to the on-demand core record.
  * Regions match regardless of case (`US-CENTRAL1` matches `us-central1`).
  * A multi-region (`US`, `EU`, `ASIA`) is priced on that area's own record
    (`pricing.AREA_PLACEMENTS`), never on a single region's.
  * If two records match, the row is refused rather than one being picked.

## BigQuery sizing, and what `--read-bq-storage` buys

Each table is sized by exactly one read:

* **By default:** `bigquery.tables.get` for each table. This is a metadata read and uses no query
  slots. It may use at most half of the run's remaining request budget (`guard.MAX_REQUESTS`) and
  stops after three refusals, counting the tables it did not reach.
* **With `--read-bq-storage`:** one query per dataset location against
  `` `<project>`.`region-<location>`.INFORMATION_SCHEMA.TABLE_STORAGE ``. Single regions and
  multi-regions both work. It returns the size, the active/long-term split and
  `storage_last_modified_time` for every table in that location, matched on `table_schema` and
  `table_name`. It needs `bigquery.jobs.create` and is billed to the project. Each query sets
  `maximumBytesBilled` to 100 MiB (`collect_bigquery.MAX_BYTES_BILLED`): BigQuery fails a query
  that would bill more, without charging for it, and `gca` records the failure.
  * If a query is refused, does not finish, or returns partial results, its tables stay unsized and
    the failure is recorded. `gca` does not fall back to `tables.get` for them.
  * A dataset whose `location` is not a well-formed id is never put into the SQL; its tables are
    sized with `tables.get`.

## Not read, and why

The README lists the services `gca` does not read, and [`API-EVIDENCE.md`](API-EVIDENCE.md) §APIs
not read gives the scope reason for each. Two further omissions are design choices:

* **Invoices.** Only the Cloud Billing export shows what was actually charged.
* **History and advice.** There are no diffs, trends, alerts, waste rules or deletion advice. The
  table lists what exists and what it costs; deciding what to delete is up to a person.

## Open questions

These need someone with access to the service account; nothing in the repo can answer them.

1. **The `TABLE_STORAGE` query has not yet run live in its current form.** The region qualifier
   and column names follow Google's documentation (quoted in `API-EVIDENCE.md`). An earlier,
   incorrect version reached the server and got `NOT_FOUND` rather than a permission error, which
   suggests `cloud-platform.read-only` allows the job but does not prove it. Try it on one project
   first.
2. **How `storage_last_modified_time` appears in `jobs.query` results.** The REST API usually
   returns `TIMESTAMP` values as epoch seconds, sometimes in exponent form (`1.7277936E9`), and
   `model.date_only` handles both. Confirm on the first live run: a value that cannot be parsed is
   kept in `evidence` as `date_unparsed`, and the row falls back to `creationTime`.
3. ~~Which scopes does a token from the service-account key actually carry?~~ **Answered
   2026-10-28:** exactly the five. A fresh service account's key minted a token that layer 1
   read back as `ENFORCED — 5 granted scopes`.
4. Can the service account get a `container.read-only` token? If so, GKE clusters could become rows.
5. Can the billing export be read with `cloud-platform.read-only`? If so, invoice-based costs could
   sit alongside `CATALOG` prices.
6. Do free tiers apply per billing account (the catalog's `aggregationLevel: ACCOUNT`)? If so, each
   row's own free tier understates the cost, and a hand-made `SUM()` is a range, not a number.
7. **Physical-storage billing.** A dataset with `storageBillingModel = PHYSICAL` is billed on
   physical bytes at different rates. Both `tables.get` (`numPhysicalBytes`) and `TABLE_STORAGE`
   (`*_physical_bytes`) report them. This is not modelled yet.
8. **GPU and Spot record names on a live price list.** The Spot core and memory names, the
   on-demand GPU names and the T4 Spot GPU name were copied from Google's public SKU pages
   (2026-10-27). Spot names for other GPU models are assumed to follow the T4 pattern. A wrong name
   cannot misprice a row, because a missing record means a refusal, but the first live run should
   show these rows as `MEASURED`. Local SSD pricing is not modelled.
9. **`projects.search` is eventually consistent**, according to its documentation, so a project
   created minutes before a run may be missing from `--all`. Name it with `--project` to be sure.
10. ~~Service-account impersonation.~~ **Answered 2026-10-28:** yes. With credentials from
    `gcloud auth application-default login --impersonate-service-account`, layer 1 read back
    `ENFORCED — 5 granted scopes`, and the run matched the key-based run row for row.
