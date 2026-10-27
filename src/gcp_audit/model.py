"""The shapes every module codes against: one resource, one money tag, one table row.

* **Unobserved is not zero.** `None` means nobody read it; `0` means the read answered zero.
* **A dollar carries its date and source.** `Money` needs `as_of` and `sku`; it is an estimate at
  a dated list price, not an invoice.
* **Storage is priced in GiB (2**30).** There is no decimal-GB helper (+7.37 %). Persistent disks
  are the exception: `disks.sizeGb` is already the billing unit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

#: The single GiB definition. No decimal-GB constant exists in this package.
GIB = 1024 ** 3

#: How a multi-valued field is flattened into `Resource.extra`; shared by collectors and pricing.
EXTRA_LIST_SEP = ","

#: Hours per month for turning an hourly rate monthly (Google's own example). Defined once so
#: pricing and the README glossary state the same convention.
HOURS_PER_MONTH = Decimal(730)
HOURS_TOKEN = f"@{HOURS_PER_MONTH}h/mo"
MONEY_CONVENTION = (f"hourly list rates integrated at {HOURS_PER_MONTH} h/month "
                    "(a convention, not a measurement)")

#: What `Row.amount_state` can hold. Only MEASURED carries a number:
#: MEASURED (size x dated price), UNSIZED (no size read), UNPRICED (no honest rate),
#: REFUSED (the read that would settle it failed).
MEASURED, UNSIZED, UNPRICED, REFUSED = "MEASURED", "UNSIZED", "UNPRICED", "REFUSED"
ROW_STATES = (MEASURED, UNSIZED, UNPRICED, REFUSED)

#: A cell nobody observed. Never blank or `0.00`: a blank reads as zero, and zero as free. `NA`
#: is what R and pandas read as missing by default. It is also a valid resource id, so a
#: collector never sends a request built from it (see `collect_bigquery`).
UNKNOWN = "NA"

#: Ids that go into URL paths and CSV cells. Strict on purpose: a misfit stops, it is not escaped.
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_./+=-]{0,512}$")
LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


def now_utc() -> str:
    """The run's timestamp, second-resolution, `Z` suffixed, so two artifacts agree."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Money:
    """One estimate: measured quantity x dated list price, in one currency.

    `sku` is the Billing Catalog SKU; `as_of` is its `effectiveTime`, which the catalog re-stamps
    daily, so it dates the sheet, not the price. `Decimal`, never float.
    """

    usd_per_month: Decimal
    sku: str
    as_of: str
    currency: str = "USD"
    unit: str = ""            # what was multiplied, e.g. "gibibyte month", "hour"

    def to_cell(self) -> str:
        return f"{self.usd_per_month.quantize(Decimal('0.01')):.2f}"


@dataclass(frozen=True)
class Unpriced:
    """Why pricing declined; printed as `amount_state` plus `evidence`, never as a zero."""

    state: str                # UNSIZED | UNPRICED | REFUSED
    reason: str               # short, lowercase, no sentence punctuation needed
    observed: str = ""        # what we did measure, e.g. "412 GiB", for the reader's benefit


@dataclass
class Resource:
    """One thing in a project, as this pass saw it. Measurements stay `None` unless read."""

    project: str
    service: str              # "compute.googleapis.com"
    kind: str                 # "Instance", "Disk", "Bucket", "Table"
    name: str
    location: str = ""
    state: str = ""           # RUNNING / ACTIVE / ENABLED / "" when the API said nothing
    size_bytes: int | None = None
    size_gb_api: int | None = None      # disks: `sizeGb` is the billing unit already
    cpus: int | None = None
    memory_mb: int | None = None
    extra: dict[str, str] = field(default_factory=dict)
    last_activity: str | None = None    # ISO-8601 date, e.g. "2026-09-30"
    activity_field: str | None = None   # which field answered: "lastStartTimestamp", ...
    size_source: str = ""               # "disks.aggregatedList", "TABLE_STORAGE", ...

    def evidence(self) -> dict[str, int | float | str]:
        """The facts behind this row, one key per observed field. Absent means unobserved."""
        out: dict[str, int | float | str] = {}
        if self.size_bytes is not None:
            out["bytes"] = self.size_bytes
            out["gib"] = round(self.size_bytes / GIB, 2)
            # Which read produced the size the money is multiplied by.
            if self.size_source:
                out["size_source"] = self.size_source
        if self.size_gb_api is not None:
            out["size_gb"] = self.size_gb_api
        # BigQuery's two storage rates apply to two quantities; a measured 0 is kept, unlike below.
        for key, name in (("active_logical_bytes", "active_gib"),
                          ("long_term_logical_bytes", "long_term_gib")):
            if self.extra.get(key) is not None:
                out[name] = round(self.extra[key] / GIB, 2)
        if self.cpus is not None:
            out["cpus"] = self.cpus
        if self.memory_mb is not None:
            out["memory_mb"] = self.memory_mb
        for key in ("machine_type", "storage_class", "source_disk", "dataset", "sku_family",
                    "attached_to", "storage_bytes_status", "not_priced", "not_measured",
                    "date_unparsed",
                    # What a VM carries that the core+RAM price does not cover (`pricing._instance`).
                    "gpus", "gpu_type", "provisioning_model", "local_ssd", "shared_cpu",
                    "network_tier", "ip_version",
                    # A snapshot's storage places, which decide whether it can be priced;
                    # `storage_locations_status` records an empty array, which this filter drops.
                    "storage_locations", "storage_locations_status"):
            if self.extra.get(key):
                out[key] = self.extra[key]
        return out


@dataclass
class Row:
    """One line of the table. `MEASURED` requires money and every other state forbids it."""

    project: str
    service: str
    kind: str
    item: str
    location: str
    state: str
    money: Money | None
    amount_state: str
    last_activity: str | None
    activity_field: str | None
    evidence: dict[str, int | float | str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.amount_state not in ROW_STATES:
            raise ValueError(f"amount_state {self.amount_state!r} is not one of {ROW_STATES}")
        if self.amount_state == MEASURED and self.money is None:
            raise ValueError("a MEASURED row must carry money")
        if self.amount_state != MEASURED and self.money is not None:
            raise ValueError(f"a {self.amount_state} row may not carry money")

    @classmethod
    def of(cls, res: Resource, money: Money | None, refusal: Unpriced | None) -> Row:
        evidence = res.evidence()
        if refusal is not None:
            evidence["reason"] = refusal.reason
            if refusal.observed:
                evidence["observed"] = refusal.observed
        if money is not None and money.sku:
            # The catalog record the price came from, so a reader can check it.
            evidence["sku"] = money.sku
        return cls(
            project=res.project, service=res.service, kind=res.kind, item=res.name,
            location=res.location or UNKNOWN, state=res.state or UNKNOWN,
            money=money,
            amount_state=MEASURED if money is not None else (refusal.state if refusal
                                                             else UNPRICED),
            last_activity=res.last_activity, activity_field=res.activity_field,
            evidence=evidence,
        )


@dataclass(frozen=True)
class Gap:
    """One read that did not answer; printed in README.txt against the read, not on rows."""

    project: str
    service: str
    read: str                 # the method id that failed, e.g. "compute.disks.aggregatedList"
    reason: str               # what the cloud said, or "not enumerated in v1"


@dataclass
class Collect:
    """What one collector found in one project, plus the reads that failed.

    No resources and no gaps means there was nothing; no resources with a gap means unknown.
    """

    service: str
    resources: list[Resource] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def refused(self, read: str, reason: str, project: str) -> Collect:
        self.gaps.append(Gap(project=project, service=self.service, read=read, reason=reason))
        return self


def format_evidence(evidence: dict[str, int | float | str]) -> str:
    """`key=value;key=value`, keys sorted; never an empty cell."""
    if not evidence:
        return "no-evidence-recorded"
    return ";".join(f"{k}={evidence[k]}" for k in sorted(evidence))


_EPOCH_FLOAT = re.compile(r"^[0-9]+\.[0-9]*(?:[eE][+-]?[0-9]+)?$|^[0-9]+[eE][+-]?[0-9]+$")


def date_only(value: object) -> str | None:
    """RFC3339, epoch seconds or epoch millis to `YYYY-MM-DD`; None if absent or unparseable,
    never 1970-01-01. Epoch seconds may carry a fraction or an exponent (`1.7277936E9`), which is
    how BigQuery's REST API renders a TIMESTAMP cell."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and _EPOCH_FLOAT.match(value.strip()):
        value = float(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        seconds = int(value)
        if seconds > 10 ** 11:      # milliseconds since the epoch, BigQuery's unit
            seconds //= 1000
        if seconds <= 0 or seconds > 4102444800:  # no time, before 1970, or after 2100
            return None
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d")
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:                                    # covers trailing-Z offsets like +00:00Z
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return None


#: The header, pinned byte for byte: spreadsheet formulas use these names. Glossary: README.txt.
CSV_HEADER = ("project,service,kind,item,location,state,usd_per_month,price_tier,"
              "price_as_of,currency,amount_state,last_activity,last_activity_field,evidence")


def cells(row: Row) -> list[str]:
    """The row's cells. Money is numeric and currency its own column, so `SUM()` works."""
    return [
        row.project, row.service, row.kind, row.item, row.location, row.state,
        row.money.to_cell() if row.money else UNKNOWN,
        "CATALOG" if row.money else UNKNOWN,
        row.money.as_of if row.money else UNKNOWN,
        row.money.currency if row.money else UNKNOWN,
        row.amount_state,
        row.last_activity or UNKNOWN,
        row.activity_field or "not-read",
        format_evidence(row.evidence),
    ]
