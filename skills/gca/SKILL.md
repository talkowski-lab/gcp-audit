---
name: gca
description: Run and interpret gca (gcp-audit), a read-only tool that lists the billable resources in a set of Google Cloud projects with their monthly list-price cost and last-used date. Use when asked to audit GCP costs, find idle or forgotten cloud resources, explain a gca run's resources.csv or README.txt, work out why a project or resource is missing from a run, or set up the auditor service account with `gca setup`.
---

# Using gca

`gca` writes one spreadsheet row per billable Google Cloud resource: what it is, what it costs per
month at list price, and when it was last used. It is **read-only**: it never changes anything in
the cloud. To change gca's own code, read `AGENTS.md` and `CONTRIBUTING.md` instead of this file.

## Rules for agents

* **Ask before running anything that costs money or changes IAM.** `gca table` is read-only and
  free, except `--read-bq-storage`, which runs one small BigQuery query per dataset location
  (cents, capped at 100 MiB billed per query). The script that `gca setup` writes changes IAM:
  never run its `create`, `key`, `impersonate` or `grant --apply` steps without the user's
  explicit go-ahead for that step. `find` and `grant` (no `--apply`) only read.
* **Never widen the credential.** gca refuses tokens with the broad `cloud-platform` scope. Do not
  try to work around a refusal; report it.
* **Do not treat the costs as a bill.** They are run-rate estimates at list price, with no
  discounts, egress or free-tier pooling. The real bill is in the Cloud Billing export.
* **Do not recommend deleting things on cost alone.** "Last used" dates are weak signals (see
  below). Present findings; the user decides.

## Commands

```bash
gca table --project ID [--project ID2 ...] -o DIR      # named projects
gca table --folder NUM [--label KEY=VALUE] -o DIR     # a folder, optionally filtered by label
gca table --all -o DIR                                # every project the service account can see
gca table ... --include-terminated                    # also list stopped VMs
gca table ... --read-bq-storage                       # BigQuery sizes and last-write dates via a query (ask first)
gca guard-check                                       # print the allowlist and scopes; sends nothing
gca setup                                             # write gca-setup.sh for creating/granting the service account
```

`gca` runs as a service account. Its key is set with `export GOOGLE_APPLICATION_CREDENTIALS=...`.
Exit codes: `0` success, `1` error, `2` refused (bad scopes, bad input, or no projects resolved).
If `gca` is not on the PATH, use `.venv/bin/gca` or `.venv/bin/python -m gcp_audit.cli` from the
repo.

## Reading the output

`-o DIR` gets two files.

**`resources.csv`**: one row per resource.

| column | meaning |
|---|---|
| `project`, `service`, `kind`, `item`, `location`, `state` | what and where. `kind` is `Instance`, `Disk`, `Image`, `Snapshot`, `Address`, `Bucket` or `Table` |
| `usd_per_month` | a number, or `NA` |
| `amount_state` | `MEASURED` (priced); `UNSIZED` (no size could be read); `UNPRICED` (sized, but no rate applies); `REFUSED` (a read failed) |
| `price_as_of` | date of the price list used |
| `last_activity`, `last_activity_field` | a date and the field it came from |
| `evidence` | `key=value;...` facts behind the row. `reason=` explains an `NA`; `sku=` names the price record |

`NA` means unknown, never zero. When totalling, sum only `MEASURED` rows and say how many rows
were not priced; a total that ignores them is a lower bound.

What the date fields mean:

* `lastStartTimestamp` (VMs): last boot. A VM running for months looks old.
* `lastAttachTimestamp` (disks): last attach. A disk never attached has no date.
* `updated` (buckets): last *metadata* change, not last data access.
* `creationTimestamp` / `creationTime`: creation only.
* `storage_last_modified_time` (tables, with `--read-bq-storage`): last data write. Nothing
  records when a table was last read.

**`README.txt`**: the run record. Read these parts:

* `projects covered:` the projects actually read. Compare with what the user expected.
* `layer1 = ...`: which account ran and with which scopes.
* `Instance rows reporting TERMINATED`: stopped VMs left out of the CSV (their disks are still in).
* `Reads that did not answer, and what they said`: one line per failed read, as
  `project  service  read  reason`. Lines with the read `not-read` and "enabled in this project and
  not enumerated by this version" only mean that the project has a service gca does not read
  (GKE, Cloud SQL, ...). Filter those out to find the real failures.

## Quick analysis

```python
import pandas as pd
df = pd.read_csv("DIR/resources.csv")          # NA cells load as missing
priced = df[df.amount_state == "MEASURED"]
priced.groupby("project").usd_per_month.sum().sort_values(ascending=False)
priced.nlargest(20, "usd_per_month")[["project", "kind", "item", "usd_per_month", "last_activity"]]
df[df.amount_state != "MEASURED"].groupby(["project", "amount_state"]).size()
```

Useful questions to answer for a user: the most expensive resources; disks with no
`lastAttachTimestamp` or a `READY` state (not attached); `RESERVED` static IPs (paid for, unused);
old snapshots; large buckets whose `updated` date is old; projects with many `UNSIZED` or
`UNPRICED` rows.

## Common problems

| symptom | cause | fix |
|---|---|---|
| a project is missing from `projects covered` | the service account has no role there, so `--all` cannot see it | grant roles (`gca setup`, then `grant`), or check with the user |
| `PERMISSION_DENIED ... Compute Engine API has not been used ... or it is disabled` | Compute Engine is off in that project | nothing to audit there; not an error to fix |
| `bigquery.jobs.query ... does not have bigquery.jobs.create`, and that project's tables are `UNSIZED` | `--read-bq-storage` without `roles/bigquery.jobUser`; gca does not fall back to per-table reads | re-run that project without `--read-bq-storage`, or grant the role |
| exit 2, "layer 1 refused" | the credential has a broad or wrong scope, e.g. a personal `gcloud` login | use the service account key; see `docs/SETUP.md` |
| a project id is rejected and nothing is written | one malformed id stops the whole run | fix the id (lowercase, 6-30 characters) |
| a project created minutes ago is missing from `--all` | project search is eventually consistent | name it with `--project` |
| `UNPRICED` VMs | local SSD, shared-core type, an unmodelled GPU or machine family | expected; `evidence` says which |

## Setting up the service account

`gca setup` asks for the user's own Google account and the service account, then writes
`gca-setup.sh`. Steps, each run by the user (or by you only with their go-ahead for that step):

```bash
./gca-setup.sh create          # create the service account if missing        (changes IAM)
./gca-setup.sh key             # key file in ~/.gca/                          (changes IAM)
./gca-setup.sh find            # projects where the user can grant roles -> grantable.txt (read-only)
./gca-setup.sh grant           # dry run                                       (read-only)
./gca-setup.sh grant --apply   # grant the read-only roles                     (changes IAM)
```

Granting needs Owner or Project IAM Admin on a project; Editor is not enough. Before `--apply`,
show the user `grantable.txt` so they can remove projects that should not be audited.

## More detail

* `README.md`: overview and coverage
* `docs/SETUP.md`: roles and setup by hand
* `docs/DESIGN.md`: how each number and date is derived, and open questions
* `docs/SAFETY.md`: the read-only guarantees
