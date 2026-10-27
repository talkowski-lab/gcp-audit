# gcp-audit (`gca`)

`gca table` reads a set of Google Cloud projects and writes `resources.csv`, a spreadsheet with
one row per billable resource. Each row says what the resource is, what it costs per month at list
price, and when it was last used, as far as Google's APIs can tell. Open it in a spreadsheet,
filter by project or cost, and share it.

`gca` is **read-only by design**:

* It refuses to run unless every OAuth scope on its token is read-only.
* It can send only twenty named requests.
* A lint check fails any source file that could reach the network any other way.

[`docs/SAFETY.md`](docs/SAFETY.md) explains each of these checks and what they do not cover.

## Install

```bash
pip install .
```

Requires Python 3.11 or later. The only runtime dependencies are `google-auth` and `requests`.

Next, set up the read-only service account. `gca setup` asks a few questions and writes a script
of `gcloud` commands that creates the account and grants its roles in every project you choose.
`gca` itself sends nothing; you review and run the script. See [`docs/SETUP.md`](docs/SETUP.md).

## Run

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/keys/gcp-audit-key.json

gca table --project lab-seq-prd --project lab-seq-dev -o ~/reports/
gca table --folder 123456789012 --label team=genomics -o ~/reports/
gca table --folder 123456789012 --all -o ~/reports/
gca table --all -o ~/reports/                            # every project the account can see
gca table --project lab-seq-prd --read-bq-storage        # size BigQuery tables with a query
gca table --project lab-seq-prd --include-terminated     # also list stopped VMs
gca guard-check                                          # print the safety policy; sends nothing
gca setup                                                # write the service-account setup script
```

You can combine named projects, a folder, a label (`KEY=VALUE`) and `--all`. With `--folder`,
`--all` and `--label` apply inside that folder; without it, they search every project the account
can see. Only `ACTIVE` projects are read.

`--read-bq-storage` runs one small query per dataset location. It costs at most a few cents, and
each query is capped at 100 MiB billed.

## Output

`-o DIR` gets two files: `resources.csv` and `README.txt`.

### `resources.csv`

Each column holds one fact, so `SUM()` and filters work. These rows come from the test suite, so
the rates are test values rather than current list prices:

```
project,service,kind,item,location,state,usd_per_month,price_tier,price_as_of,currency,amount_state,last_activity,last_activity_field,evidence
lab-seq-prd,compute.googleapis.com,Instance,wdl-worker,us-central1-a,RUNNING,141.79,CATALOG,2026-09-01,USD,MEASURED,2026-09-27,lastStartTimestamp,cpus=4;machine_type=e2-standard-4;memory_mb=16384;sku=E2-CORE+E2-RAM
lab-seq-prd,compute.googleapis.com,Disk,pd-forgotten,us-central1-a,READY,20.00,CATALOG,2026-09-01,USD,MEASURED,NA,not-read,size_gb=500;sku=PD-STD;storage_class=pd-standard
lab-seq-prd,compute.googleapis.com,Address,ip-idle,us-central1,RESERVED,7.29,CATALOG,2026-09-01,USD,MEASURED,2025-03-04,creationTimestamp,ip_version=IPV4;sku=66A2-68EA-56BE
lab-seq-prd,compute.googleapis.com,Address,ip-web,us-central1,IN_USE,NA,NA,NA,NA,UNPRICED,2025-03-04,creationTimestamp,"attached_to=web;reason=in use: billed as an external IP of the resource using it, not priced here"
lab-seq-prd,storage.googleapis.com,Bucket,lab-raw,europe-west1,NA,6891.79,CATALOG,2026-09-01,USD,MEASURED,2026-06-30,updated,bytes=740000000000000;gib=689178.71;size_source=storage.googleapis.com/storage/total_bytes;sku=NEARLINE-EUW1;storage_class=NEARLINE
lab-seq-prd,storage.googleapis.com,Bucket,lab-tmp,us-central1,NA,NA,NA,NA,NA,UNSIZED,2026-09-01,updated,"not_measured=byte gauge asked about 7 days emitted no series;reason=the bucket holds objects and the byte gauge was silent, so its bytes are unobserved;storage_class=STANDARD"
lab-seq-prd,bigquery.googleapis.com,Table,alignments,EU,NA,0.20,CATALOG,2026-09-01,USD,MEASURED,2023-11-03,creationTime,active_gib=20.0;bytes=32212254720;dataset=eu_raw;gib=30.0;long_term_gib=10.0;not_measured=TABLE_STORAGE not read (--read-bq-storage);size_source=bigquery.tables.get/numBytes;sku=947D-3B46-7781+993F-6B6B-DCC4
```

| column | meaning |
|---|---|
| `usd_per_month` | measured quantity × one dated Billing Catalog list price, or `NA` |
| `amount_state` | `MEASURED`, or why there is no number: `UNSIZED` (no size was read), `UNPRICED` (no rate applies), `REFUSED` (a read failed) |
| `price_tier`, `price_as_of`, `currency` | always `CATALOG`; the date of the price list; the currency |
| `last_activity`, `last_activity_field` | a date, and the field it came from. The fields mean different things: `lastStartTimestamp` is the last boot, a bucket's `updated` is the last metadata change, and `creationTime` is only a lower bound |
| `evidence` | `key=value` pairs for the facts behind the row, including the price record (`sku`) |

A cell with no value is `NA`, never blank and never `0.00`. R and pandas treat `NA` as missing; in
a spreadsheet it is text, which `SUM()` skips.

### `README.txt`

A record of the run: when it ran, which projects it covered, the account and scopes it ran with,
every read that failed and the API's reason, and the enabled services that `gca` does not read.

## Coverage

| service | rows | size from |
|---|---|---|
| Compute Engine | VMs, persistent disks, custom images, snapshots, external static IP addresses | machine type; `sizeGb`; `storageBytes` (snapshots). Images are listed but not sized |
| Cloud Storage | buckets | the Cloud Monitoring `storage/total_bytes` metric |
| BigQuery | tables | `tables.get` by default (free), or `INFORMATION_SCHEMA.TABLE_STORAGE` with `--read-bq-storage` |

VMs in the `TERMINATED` state are left out by default, because a stopped VM is not charged for CPU
or memory. `README.txt` counts them, and their disks still get rows.

**Priced:**

* VMs in the `e2`, `n1`, `n2` and `n2d` families, at on-demand or Spot rates, including attached
  NVIDIA T4, P4, P100, V100, A100 and A100 80GB GPUs
* unattached (`RESERVED`) external IPv4 addresses
* persistent disks, snapshots, buckets and BigQuery tables (logical storage)

**Listed, but `UNPRICED`,** with the reason in `evidence`:

* VMs with local SSDs, shared-core machine types (such as `e2-micro`), other GPU models, more than
  one GPU model, or another machine family
* external addresses in use, which are billed as part of the VM or load balancer using them

BigQuery tables billed on physical storage are priced as if billed on logical storage. Check those
by hand.

**Not read:** GKE, Cloud Run, Cloud Functions, Pub/Sub, Dataproc, Artifact Registry, Cloud SQL and
Spanner, because their APIs have no read-only OAuth scope and `gca` will not hold a writable token.
GKE nodes still appear as VMs. Filestore, load balancers, NAT and network egress are not read
either. Internal IP addresses are free, so they get no rows. `README.txt` lists every enabled
service that was not read.

## Limits

The costs are a **run-rate estimate at list price**, not an invoice. They leave out committed-use
and sustained-use discounts, negotiated prices, free tiers beyond those in the price list, and
network egress. For the actual bill, use the Cloud Billing export, which `gca` does not read.

`gca` never changes anything in your projects, and no option makes it. It does not delete, resize
or recommend anything. It reports what exists, what it costs, and when it was last used.

## Documentation

| | |
|---|---|
| [`docs/SETUP.md`](docs/SETUP.md) | creating the service account and granting its roles |
| [`docs/SAFETY.md`](docs/SAFETY.md) | the three read-only layers, and what they do not check |
| [`docs/DESIGN.md`](docs/DESIGN.md) | what a row means, where each number and date comes from, open questions |
| [`docs/API-EVIDENCE.md`](docs/API-EVIDENCE.md) | each request, its scope, and the API facts the code relies on |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | code layout, test commands, and rules for changes |
| [`skills/gca/SKILL.md`](skills/gca/SKILL.md) | a guide for AI agents: running gca and reading its output |

## License

MIT. See [`LICENSE`](LICENSE).
