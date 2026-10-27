# Contributing

## Layout

```
src/gcp_audit/
  cli.py               `gca table`, `gca guard-check`, `gca setup`; ties the parts together
  setup_script.py      `gca setup`: writes a gcloud script for the user to run; sends nothing
  guard.py             layer 1 (scopes via tokeninfo) and the only network code
  methods.py           layer 2: the allowlisted requests
  projects.py          turns --project / --folder / --label / --all into project ids
  collect_compute.py   VMs, disks, images, snapshots, machine types, IP addresses
  collect_storage.py   buckets, sized by the Monitoring metric
  collect_bigquery.py  tables, sized by tables.get or TABLE_STORAGE
  pricing.py           Billing Catalog SKUs -> Money, or a named reason it can't be priced
  model.py             Resource, Money, Row, CSV_HEADER, and the amount states
  table.py             writes resources.csv and README.txt
tools/lint_readonly.py layer 3: the lint check
tests/                 offline; conftest.py blocks the network
```

Collectors return a `model.Collect` (resources plus gaps) and never raise when the cloud refuses a
read. `pricing.price(resource, rates)` returns either `(Money, None)` or `(None, Unpriced)`.

## Checks

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                          # tests
.venv/bin/python tools/lint_readonly.py      # read-only lint; nonzero exit means failure
.venv/bin/ruff check src tests tools         # style
```

Run the venv binaries by path. Judge each check by its exit status, not its last line of output
(`cmd > out 2>&1; echo EXIT=$?`). CI (`.github/workflows/gates.yml`) runs the same three.

## Rules

**Never change a cloud resource.** Nothing may weaken the three layers in
[`docs/SAFETY.md`](docs/SAFETY.md). These changes need an explicit decision from a maintainer, not
a drive-by edit:

* a new entry in `methods.py`
* a new scope
* a new dependency
* a change to a pinned value: `model.CSV_HEADER`, `methods.READ_COUNT`, or the lint's allow and
  import counts

**Tests never use the network or real credentials.** Tests install a fake session with
`guard.install()`. It subclasses `guard.GuardedSession` and replaces only `_dispatch`, so the
admission checks under test are the real ones. Nothing in `src/` runs `gcloud`, `gsutil`, `bq` or
`curl`.

**Unknown is not zero.** `None` means nothing was observed; `0` means an API reported zero. A failed
read becomes a `Gap` carrying the API's reason, never an empty list, a zero or a missing row.
`amount_state` is one of `MEASURED`, `UNSIZED`, `UNPRICED` or `REFUSED`. `Row` rejects an `NA` row
that carries a number, and a priced row without its SKU and date.

**The table.** One fact per column. No cell holds a sentence, and no unit sits next to a number. A
cell with no value is `NA`. A key nobody observed is left out of `evidence`. Nothing is printed
above the header, and there is no totals row.

**Money.** Every cost comes from a dated Billing Catalog SKU (`price_as_of`, and the SKU id in
`evidence`). Storage is billed per GiB (2³⁰ bytes), except persistent disks, whose `sizeGb` is
already the billing unit. Snapshots are priced on `storageBytes`. No discount is modelled without
an invoice. If a resource is billed for something the formula does not cover, return `UNPRICED`
and name it. Never print a partial price.

**Untrusted strings.** Names, labels and locations from the cloud go only into table cells, never
into a file path, a shell command or SQL. The one SQL statement is a constant; only a
pattern-checked project id and location are inserted into it.

**Don't guess API shapes.** Before relying on a field, find it in the API's discovery document
(`https://<api>/$discovery/rest?version=<v>`). Record the field, and what the code uses it for, in
[`docs/API-EVIDENCE.md`](docs/API-EVIDENCE.md).

**Keep comments short and give the reason.** History belongs in commit messages, not in comments
or docstrings.
