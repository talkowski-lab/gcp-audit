"""pricing.py — the dated price sheet, and the only place in this package that makes a number.

`load_rates` reads the Billing Catalog (`cloudbilling.services.list`, then `services.skus.list`
paged in full; it has no server-side filter) and keeps the records in force on `as_of`. `price`
multiplies one `model.Resource`'s measured quantity by one of those rates. Rules:

* **An undated price is not a price.** A record's period runs from its `effectiveTime` to the next
  entry's; a record with no date is refused at load. The catalog re-stamps every record at each
  daily publication, so `Money.as_of` dates the sheet rather than the rate (`docs/API-EVIDENCE.md`).
* **A bare number declares no scale.** `unitPrice` is `units` + `nanos`/10⁹; `0.02` is refused.
* **Storage is GiB** (`model.GIB` = 2**30); a decimal-GB division overprices by +7.37 %. Persistent
  disks are the exception: `disks.sizeGb` is already the billing unit.
* **A machine type has no whole-type SKU**: one core record plus one or two RAM records (custom
  `-ext` shapes split RAM at the family's ceiling). Missing any one refuses the whole row.
* **Exactly one candidate rate, or nothing.** Zonal/regional, single/dual-region and standard/
  Autoclass records coexist in one region at 2x-20x apart; two matches is a refusal, not a tie-break.
* **Region comparison folds case**; the row still prints the cloud's spelling. An area (`US`, `EU`,
  `ASIA`) is never folded into a region: it is priced on the area's own record (`AREA_PLACEMENTS`).
  A dual-region names no single record and matches nothing.
* **A record is chosen by the anchored head of its description** (`_describes`), never a substring:
  `Autoclass Standard Storage …` and `Regional …` records are different products at different prices.

No CUD or sustained-use discount is modelled, nothing is converted between currencies, and a tiered
ladder is summed the way billing sums it (`Rate.marginal`).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from gcp_audit import guard, methods, model

#: Hours-per-month convention (730, Google's own example). Machine SKUs are hourly, so a monthly
#: figure is an assumption; it travels in `Money.unit` and the README glossary. Defined in `model`
#: so the money cell and the glossary cannot disagree.
HOURS_PER_MONTH = model.HOURS_PER_MONTH
HOURS_TOKEN = model.HOURS_TOKEN
MONEY_CONVENTION = model.MONEY_CONVENTION

#: `Money.nanos` divisor. Spelled as a power so it is not mistaken for a decimal-gigabyte literal.
NANO_DIVISOR = Decimal(10) ** 9

#: `machineTypes.memoryMb` is in MiB and RAM is priced per GiB, so 1024, not 1000.
MB_PER_GIB = Decimal(1024)

#: The `usageUnit` codes this module will multiply. Any other unit (`GiBy.d`, `count`, `By.s`) has
#: a relation to a month this module will not guess.
UNIT_HOUR = "h"
UNIT_GIB_HOUR = "GiBy.h"
UNIT_GIB_MONTH = "GiBy.mo"

#: Catalog sheets, matched exactly on `Service.displayName`. Exact matching matters: the catalog
#: names several other services "BigQuery …", and a substring match would price off the wrong sheet.
SHEET_COMPUTE = "Compute Engine"
SHEET_STORAGE = "Cloud Storage"
SHEET_BIGQUERY = "BigQuery"
NEEDED_SHEETS = (SHEET_COMPUTE, SHEET_STORAGE, SHEET_BIGQUERY)

#: Page size for every catalog read; the Compute Engine sheet is ~33k records.
PAGE_SIZE = 1000

#: Which sheet each priced kind is looked up in; v1 prices these seven kinds and every other kind is
#: `UNPRICED`. `Dataset` is absent on purpose: pricing it would count its tables' bytes twice.
KIND_SHEET = {"Instance": SHEET_COMPUTE, "Disk": SHEET_COMPUTE, "Snapshot": SHEET_COMPUTE,
              "Image": SHEET_COMPUTE, "Address": SHEET_COMPUTE, "Bucket": SHEET_STORAGE,
              "Table": SHEET_BIGQUERY}

@dataclass(frozen=True)
class _MachineRates:
    """The five description heads one machine family's records are filed under.

    Each is copied verbatim from the live Compute Engine sheet, quirks included (N1's custom records
    carry no family prefix; N2D's extended record lacks the word `Instance`). No extended record has
    been read for E2, so `custom_extended` is empty and an E2 `-ext` shape refuses.
    """

    core: str
    ram: str
    custom_core: str
    custom_ram: str
    custom_extended: str        # "" when no such record has been read for this family


#: Machine families priced here, keyed by the first segment of the machine type (`n1-standard-8` ->
#: `n1`). Any other family is `UNPRICED`: inventing a description phrase is inventing a rate.
MACHINE_FAMILIES: dict[str, _MachineRates] = {
    "e2": _MachineRates("E2 Instance Core", "E2 Instance Ram",
                        "E2 Custom Instance Core", "E2 Custom Instance Ram", ""),
    "n1": _MachineRates("N1 Predefined Instance Core", "N1 Predefined Instance Ram",
                        "Custom Instance Core", "Custom Instance Ram",
                        "Custom Extended Instance Ram"),
    "n2": _MachineRates("N2 Instance Core", "N2 Instance Ram",
                        "N2 Custom Instance Core", "N2 Custom Instance Ram",
                        "N2 Custom Extended Instance Ram"),
    "n2d": _MachineRates("N2D AMD Instance Core", "N2D AMD Instance Ram",
                         "N2D AMD Custom Instance Core", "N2D AMD Custom Instance Ram",
                         "N2D AMD Custom Extended Ram"),
}

#: The same heads for a Spot or preemptible VM, verbatim from Google's public SKU groups
#: (cloud.google.com/skus/sku-groups/spot-preemptible-{e2,n1,n2,n2d}-vms, read 2026-10-27; N1 also
#: in the 2026-09-29 catalog capture). N2D's Spot extended record DOES carry `Instance`. No Spot
#: custom record is listed for E2, so a custom E2 Spot VM refuses.
SPOT_FAMILIES: dict[str, _MachineRates] = {
    "e2": _MachineRates("Spot Preemptible E2 Instance Core", "Spot Preemptible E2 Instance Ram",
                        "", "", ""),
    "n1": _MachineRates("Spot Preemptible N1 Predefined Instance Core",
                        "Spot Preemptible N1 Predefined Instance Ram",
                        "Spot Preemptible Custom Instance Core",
                        "Spot Preemptible Custom Instance Ram",
                        "Spot Preemptible Custom Extended Instance Ram"),
    "n2": _MachineRates("Spot Preemptible N2 Instance Core", "Spot Preemptible N2 Instance Ram",
                        "Spot Preemptible N2 Custom Instance Core",
                        "Spot Preemptible N2 Custom Instance Ram",
                        "Spot Preemptible N2 Custom Extended Instance Ram"),
    "n2d": _MachineRates("Spot Preemptible N2D AMD Instance Core",
                         "Spot Preemptible N2D AMD Instance Ram",
                         "Spot Preemptible N2D AMD Custom Instance Core",
                         "Spot Preemptible N2D AMD Custom Instance Ram",
                         "Spot Preemptible N2D AMD Custom Extended Instance Ram"),
}

#: Provisioning models billed on the Spot records. Others (FLEX_START, RESERVATION_BOUND) refuse.
SPOT_MODELS = frozenset({"SPOT", "PREEMPTIBLE"})

#: Accelerator type -> on-demand GPU record head, verbatim from the public `on-demand-gpus` SKU
#: group (read 2026-10-27). A type not listed refuses rather than borrow a neighbour's rate.
GPU_MODELS: dict[str, str] = {
    "nvidia-tesla-t4": "Nvidia Tesla T4 GPU",
    "nvidia-tesla-p4": "Nvidia Tesla P4 GPU",
    "nvidia-tesla-p100": "Nvidia Tesla P100 GPU",
    "nvidia-tesla-v100": "Nvidia Tesla V100 GPU",
    "nvidia-tesla-a100": "Nvidia Tesla A100 GPU",
    "nvidia-a100-80gb": "Nvidia Tesla A100 80GB GPU",
}

#: A GPU on a Spot VM is its own record: `<head> attached to Spot Preemptible VMs running in …`
#: (verbatim for T4; assumed for the others, and a missing record refuses the row).
GPU_SPOT_SUFFIX = " attached to Spot Preemptible VMs"

#: An unattached external IPv4 address: `Static Ip Charge` / `Static Ip Charge in <city>`,
#: Network / IpAddress, per hour (2026-09-29 catalog capture, `66A2-68EA-56BE`).
STATIC_IP_PHRASE = "Static Ip Charge"

#: Machine-type name modifiers that change which records price it (e.g. `n2d-custom-2-24832-ext`).
#: The core/memory counts are parsed in `collect_compute._CUSTOM_MACHINE_TYPE`.
CUSTOM_MARKER = "-custom-"

#: Extended memory: RAM above the family's per-vCPU ceiling bills at a separate, roughly 2x rate.
EXTENDED_MARKER = "-ext"


@dataclass(frozen=True)
class _Ceiling:
    """How much RAM per vCPU counts as ordinary memory, and the sentence that says so."""

    gib_per_vcpu: Decimal
    because: str


_MACHINES_PAGE = ("https://cloud.google.com/compute/docs/general-purpose-machines, "
                  "fetched 2026-10-20")

#: Per-family memory ceiling (GiB per vCPU) above which a `-ext` shape's RAM is extended. A family
#: with no entry gets no split; adding one requires quoting the documentation sentence for it.
RAM_CEILINGS: dict[str, _Ceiling] = {
    "n2d": _Ceiling(Decimal("8.0"),
                    '"For N2D machine types, select between 0.5 GB and 8.0 GB per vCPU in 256 MB '
                    f'increments. Higher amounts of memory are possible by enabling extended memory."'
                    f' — {_MACHINES_PAGE}'),
    "n2": _Ceiling(Decimal("8.0"),
                   '"For the N2 machine series, select between 0.5 GB and 8.0 GB per vCPU in 256 MB '
                   f'increments. Higher amounts of memory are possible by enabling extended memory."'
                   f' — {_MACHINES_PAGE}'),
    "n1": _Ceiling(Decimal("6.5"),
                   '"For N1 machine types, select between 0.9 GB and 6.5 GB per vCPU, inclusive." '
                   f"— {_MACHINES_PAGE}"),
}

#: Persistent-disk classes by basename of `disks[].type`: (`resourceGroup`, description head). The
#: anchored head excludes the `Regional …` records (2x the zonal rate); `_disk` refuses regional disks.
PD_CLASSES: dict[str, tuple[str, str]] = {
    "pd-balanced": ("SSD", "Balanced PD Capacity"),
    "pd-ssd": ("SSD", "SSD backed PD Capacity"),
    "pd-standard": ("PDStandard", "Storage PD Capacity"),
}

#: Snapshot storage, priced on `storageBytes` (never the source disk's `diskSizeGb`). The record is
#: chosen by `Snapshot.storageLocations`, not the row's `location` — see `_snapshot`.
SNAPSHOT_GROUP, SNAPSHOT_PHRASE = "PDSnapshot", "Storage PD Snapshot"

#: Custom-image storage lookup. No live image SKU has been verified, so an absent record is
#: `UNPRICED`; the snapshot rate is never borrowed for an image.
IMAGE_PHRASE = "Storage PD Image"

#: Cloud Storage class -> (`resourceGroup`, capacity description head, match variants). Standard
#: capacity is in `RegionalStorage`, not `Storage` (which holds Autoclass and ops records). The head
#: is anchored so `Autoclass Standard Storage …`, a different rate table, never matches a plain
#: bucket. `REGIONAL` is the legacy spelling of regional Standard.
GCS_CLASSES: dict[str, tuple[str, str, bool]] = {
    "ARCHIVE": ("ArchiveStorage", "Archive Storage", False),
    "COLDLINE": ("ColdlineStorage", "Coldline Storage", False),
    "MULTI_REGIONAL": ("MultiRegionalStorage", "Standard Storage", True),
    "NEARLINE": ("NearlineStorage", "Nearline Storage", False),
    "REGIONAL": ("RegionalStorage", "Standard Storage", False),
    "STANDARD": ("RegionalStorage", "Standard Storage", False),
}
#: Redundancy and retention variants share a region in `serviceRegions` at 2x-20x the price, so a
#: single-region lookup excludes them rather than averaging or picking.
_VARIANT_WORDS = re.compile(r"dual-region|multi-region|early delete", re.IGNORECASE)

#: Area placement -> the key its own capacity records are offered at (`docs/API-EVIDENCE.md`).
#: A table, not `location.lower()`: `eu` is where Autoclass EU records sit, a different product.
#: Dual-regions are absent on purpose: no one record covers a pair of places.
AREA_PLACEMENTS: dict[str, str] = {"US": "us", "EU": "europe", "ASIA": "asia"}

#: Classes that may take an area placement. `REGIONAL` is regional by definition, so an area
#: location contradicts it and is refused.
AREA_CLASSES = frozenset({"STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE", "MULTI_REGIONAL"})

#: Multi-region Standard records sit in `MultiRegionalStorage`, not the class's regional group.
AREA_GROUPS: dict[str, str] = {"STANDARD": "MultiRegionalStorage"}

#: Description tails, anchored so `Regional Storage PD Capacity` and `DWS … running in …` stay out
#: of other lookups. Compute writes `in <Place>`, machine records `running in <Place>`, and Cloud
#: Storage appends the place with no preposition.
_TAIL_IN_PLACE = r"(?: in .+)?$"
_TAIL_RUNNING_IN = r"(?: running in .+)?$"
_TAIL_PLACE = r"(?: .+)?$"

#: Zone vs region decides which SKU a disk bills on. Case-insensitive because buckets report
#: locations in caps; refusals still name the API's value.
_ZONE = re.compile(r"^([a-z]+-[a-z]+[0-9]+)-[a-z]$", re.IGNORECASE)
_SINGLE_REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]+$", re.IGNORECASE)


class _BadRecord(Exception):
    """A price record this loader cannot use; counted in `Rates.rejected` rather than half-parsed."""


class _Declined(Exception):
    """Why `price` refused a row, with the `amount_state` (UNSIZED, UNPRICED or REFUSED) to print."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state, self.reason = state, reason


@dataclass(frozen=True)
class Rate:
    """One SKU's price for one period, in one currency, for a set of regions.

    `sku_id` is not unique: a price change is a second record under the same id, hence the period.
    """

    sku_id: str
    sheet: str
    description: str
    family: str
    group: str
    usage_unit: str
    #: `(startUsageAmount, price per usage unit)` per tier, ascending. One entry for a flat rate.
    tiers: tuple[tuple[Decimal, Decimal], ...]
    currency: str
    regions: tuple[str, ...]
    #: True when `geoTaxonomy.type` is GLOBAL. A record naming no region that is not global matches
    #: nothing.
    global_sku: bool
    as_of: str                # the record's own date, verbatim

    def offers(self, region: str) -> bool:
        """Does this record price `region`? Case-insensitive: the catalog and Storage disagree on case."""
        if self.global_sku:
            return True
        return any(region.casefold() == name.casefold() for name in self.regions)

    def marginal(self, quantity: Decimal) -> Decimal:
        """Cost of `quantity` units, each tier pricing the usage between its start and the next's.
        Neither the top rate nor the free bottom tier applies to the whole quantity."""
        total = Decimal(0)
        for position, (start, rate) in enumerate(self.tiers):
            if quantity <= start:
                break
            ceiling = self.tiers[position + 1][0] if position + 1 < len(self.tiers) else None
            billed_up_to = quantity if ceiling is None else min(quantity, ceiling)
            total += (billed_up_to - start) * rate
        return total


@dataclass
class Rates:
    """The price sheet. `unavailable` names each sheet that failed to load and why, so its rows
    print `REFUSED` rather than `UNPRICED`; a partially read sheet is dropped, not kept."""

    as_of: str = ""                                        # the day the sheet was asked to cover
    rates: list[Rate] = field(default_factory=list)
    unavailable: dict[str, str] = field(default_factory=dict)   # sheet -> why it is not here
    rejected: list[tuple[str, str]] = field(default_factory=list)   # (sku id, why it is unusable)

    def one(self, *, sheet: str, family: str, group: str, phrase: str, unit: str, region: str,
            label: str = "", match_variants: bool = False, where: str = "",
            tail: str = _TAIL_IN_PLACE) -> Rate:
        """The single rate for this lookup, or a refusal naming what was looked for.

        `phrase` is an anchored description head and `tail` its place suffix. `label` names the
        lookup in a refusal when `phrase` alone would not; `where` is the location as the API gave
        it. `match_variants` is set only where a variant IS the right record, never to widen a lookup.
        """
        what = label or phrase
        if not region:
            raise _Declined(model.UNPRICED, f"no dated {what} rate: location not observed"
                            if not where else f"no dated {what} rate for {where}")
        hits = [rate for rate in self.rates
                if rate.sheet == sheet
                and rate.usage_unit == unit
                and (not family or rate.family == family)
                and (not group or rate.group == group)
                and rate.offers(region)
                and _describes(rate.description, phrase, tail)
                and (match_variants or not _VARIANT_WORDS.search(rate.description))]
        if not hits:
            raise _Declined(model.UNPRICED, f"no dated {what} rate for {where or region}")
        if len(hits) > 1:
            ids = ", ".join(sorted(rate.sku_id for rate in hits))
            raise _Declined(model.UNPRICED,
                            f"{len(hits)} dated {what} rates for {where or region} ({ids}), "
                            "refusing to pick one")
        return hits[0]


def _describes(description: str, phrase: str, tail: str) -> bool:
    """Does the description start with exactly `phrase` followed by `tail`? Anchored, because a
    substring match makes `Autoclass Standard Storage …` a Standard bucket's rate."""
    if not phrase:
        return True
    return re.match("^" + re.escape(phrase) + tail, description) is not None


# --------------------------------------------------------------------- the loader

def load_rates(as_of: str | None = None) -> Rates:
    """Read the Billing Catalog and keep the records in force on `as_of` (today, UTC, if unset).

    Expired rates are dropped here since `skus.list` has no server-side filter. A failed read is
    recorded in `Rates.unavailable`, never raised, so rows print `NA` with a reason.
    """
    day = _day(as_of if as_of is not None else model.now_utc())
    rates = Rates(as_of=day)
    try:
        services = [service for page in guard.paged("cloudbilling.services.list", params={},
                                                    query={"pageSize": PAGE_SIZE})
                    for service in page.get("services") or []]
    except guard.ReadFailed as exc:
        for sheet in NEEDED_SHEETS:
            rates.unavailable[sheet] = exc.reason
        return rates

    wanted: dict[str, str] = {}
    #: sheets the catalog DID name, with the reason this build could not use the row
    unpurchasable: dict[str, str] = {}
    for service in services:
        name = str(service.get("displayName") or "")
        service_id = str(service.get("serviceId") or "")
        if name not in NEEDED_SHEETS or name in wanted:
            continue
        # `serviceId` is the bare id; `skus.list` is addressed at `services/<id>`.
        parent = service_id if service_id.startswith("services/") else f"services/{service_id}"
        if not re.match(methods.PATTERNS["service"], parent):
            rates.rejected.append((service_id or "<no serviceId>",
                                   f"serviceId {service_id!r} is not an identifier that can be "
                                   "addressed as services/<id>"))
            # No `;` in here: this string reaches `evidence`, which is `key=value;key=value`.
            unpurchasable[name] = (f"cloudbilling.services.list named {name!r} with an identifier "
                                   f"this build cannot address ({service_id!r}), the sheet was not "
                                   "read and the catalog is not the thing that failed")
            continue
        wanted[name] = parent
    for sheet in NEEDED_SHEETS:
        if sheet not in wanted:
            # Say "did not list it" only when that is true; a misleading gap sends the operator
            # after the wrong problem.
            rates.unavailable[sheet] = unpurchasable.get(
                sheet) or "cloudbilling.services.list did not list it"
    for sheet, service_id in wanted.items():
        kept: list[Rate] = []
        try:
            for page in guard.paged("cloudbilling.services.skus.list",
                                    params={"parent": service_id}, query={"pageSize": PAGE_SIZE}):
                for raw in page.get("skus") or []:
                    _read_sku(raw, sheet, day, kept, rates.rejected)
        except guard.ReadFailed as exc:
            rates.unavailable[sheet] = exc.reason
            continue                    # a partial sheet is worse than an absent one
        rates.rates.extend(kept)
    return rates


def _day(value: object) -> str:
    day = model.date_only(value)
    if day is None:
        raise guard.AuditError(f"as_of {value!r} is not a date this module can compare against")
    return day


def _read_sku(raw: Mapping[str, object], sheet: str, day: str, kept: list[Rate],
              rejected: list[tuple[str, str]]) -> None:
    """One `Sku` payload -> its in-force `Rate`s, or a counted rejection."""
    sku_id = str(raw.get("skuId") or "").strip()
    if not sku_id:
        name = str(raw.get("name") or "")
        sku_id = name.rstrip("/").rsplit("/", 1)[-1] if "/" in name else ""
    try:
        kept.extend(_sku_rates(raw, sheet, sku_id or "<unnamed sku>", day))
    except _BadRecord as exc:
        rejected.append((sku_id or "<unnamed sku>", str(exc)))


def _sku_rates(raw: Mapping[str, object], sheet: str, sku_id: str, day: str) -> list[Rate]:
    """The Rate(s) of one `Sku` whose `pricingInfo` entry is in force on `day`, or `_BadRecord`."""
    category = _mapping(raw.get("category"), what="category")
    regions, is_global = _regions(raw)
    explicit = raw.get("pricingPeriod")
    entries = raw.get("pricingInfo")
    if not isinstance(entries, list) or not entries:
        raise _BadRecord("pricingInfo is absent, so this record states no price")
    out: list[Rate] = []
    for position, entry in enumerate(entries):
        info = _mapping(entry, what=f"pricingInfo[{position}]")
        start, end = _period(info, position, entries, explicit)
        if not _covers(start, end, day):
            continue
        expression = _mapping(info.get("pricingExpression"),
                              what=f"pricingInfo[{position}].pricingExpression")
        tiers, currency = _tiers(expression, sku_id)
        out.append(Rate(
            sku_id=sku_id, sheet=sheet, description=str(raw.get("description") or ""),
            family=str(category.get("resourceFamily") or ""),
            group=str(category.get("resourceGroup") or ""),
            usage_unit=str(expression.get("usageUnit") or ""), tiers=tiers,
            currency=currency, regions=regions, global_sku=is_global, as_of=start))
    return out


def _mapping(value: object, *, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _BadRecord(f"{what} is {type(value).__name__}, not an object")
    return value


def _regions(raw: Mapping[str, object]) -> tuple[tuple[str, ...], bool]:
    """The regions a record is offered in, and whether it claims to be global. Some records
    (Cloud Storage capacity) carry only `geoTaxonomy.regions` instead of `serviceRegions`."""
    listed = raw.get("serviceRegions")
    taxonomy = raw.get("geoTaxonomy")
    if not isinstance(listed, list) or not listed:
        listed = taxonomy.get("regions") if isinstance(taxonomy, Mapping) else None
    regions = tuple(str(item) for item in listed) if isinstance(listed, list) else ()
    is_global = isinstance(taxonomy, Mapping) and taxonomy.get("type") == "GLOBAL"
    return regions, is_global


def _period(info: Mapping[str, object], position: int, entries: list[object],
            explicit: object) -> tuple[str, str | None]:
    """(start, end) of one `pricingInfo` entry, at day granularity.

    An entry runs from its `effectiveTime` to the next entry's; the last is open-ended. A
    record-level `pricingPeriod`, when present, wins as the narrower claim. Neither is refused.
    """
    if explicit is not None:
        stamp = _mapping(explicit, what="pricingPeriod")
        start = _date(stamp.get("startTime"), what="pricingPeriod.startTime")
        end = _date(stamp.get("endTime"), what="pricingPeriod.endTime") if stamp.get(
            "endTime") else None
        return start, end
    nxt = entries[position + 1] if position + 1 < len(entries) else None
    start = _date(info.get("effectiveTime"), what="pricingInfo.effectiveTime")
    end = None
    if nxt is not None:
        end = _date(_mapping(nxt, what=f"pricingInfo[{position + 1}]").get("effectiveTime"),
                    what=f"pricingInfo[{position + 1}].effectiveTime")
    return start, end


def _date(value: object, *, what: str) -> str:
    day = model.date_only(value)
    if day is None:
        raise _BadRecord(f"{what} is absent or unparsable ({value!r}), so the price is undated")
    return day


def _covers(start: str, end: str | None, day: str) -> bool:
    """Does [start, end) cover `day`? Dates are ISO, so string order is time order."""
    return start <= day and (end is None or day < end)


def _tiers(expression: Mapping[str, object], sku_id: str
           ) -> tuple[tuple[tuple[Decimal, Decimal], ...], str]:
    """(ladder, currency) from a `PricingExpression`; a bare `unitPrice` is a one-step ladder."""
    steps = expression.get("tieredRates")
    if not isinstance(steps, list) or not steps:
        steps = ([{"startUsageAmount": 0, "unitPrice": expression.get("unitPrice")}]
                 if "unitPrice" in expression else [])
    if not steps:
        raise _BadRecord(f"{sku_id}: pricingExpression carries no unitPrice and no tieredRates")
    ladder: list[tuple[Decimal, Decimal]] = []
    currency = ""
    for position, step in enumerate(steps):
        entry = _mapping(step, what=f"{sku_id} tier {position}")
        amount, step_currency = _unit_price(entry.get("unitPrice"),
                                           what=f"{sku_id} tier {position}.unitPrice")
        start = entry.get("startUsageAmount")
        if start is None:
            start = 0
        if isinstance(start, bool) or not isinstance(start, (int, float, str)):
            raise _BadRecord(f"{sku_id} tier {position}: startUsageAmount is {start!r}")
        currency = currency or step_currency
        if step_currency and step_currency != currency:
            raise _BadRecord(f"{sku_id}: its tiers mix {currency} with {step_currency}")
        ladder.append((Decimal(str(start)), amount))
    ladder.sort(key=lambda step: step[0])
    return tuple(ladder), currency


def _unit_price(money: object, *, what: str) -> tuple[Decimal, str]:
    """(price per usage unit, currency) from a `Money` message — `units` + `nanos` / 10⁹, as
    `Decimal`. Anything without integer `nanos` declares no scale and is refused."""
    if not isinstance(money, Mapping):
        raise _BadRecord(f"{what} is {type(money).__name__} ({money!r}); a bare number declares "
                         "no scale — it must carry units and nanos")
    for name in ("units", "nanos"):
        value = money.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise _BadRecord(f"{what} has {name}={value!r}, which is not an integer "
                             f"{name} — a fractional number declares no scale")
        if isinstance(value, str) and not value.strip().lstrip("-").isdigit():
            raise _BadRecord(f"{what} has {name}={value!r}, which is not an integer")
    nanos, units = money.get("nanos"), money.get("units")
    if nanos is None:
        raise _BadRecord(f"{what} carries no nanos, so it declares no scale")
    amount = Decimal(int(units or 0)) + Decimal(int(nanos)) / NANO_DIVISOR
    if amount < 0:
        raise _BadRecord(f"{what} is a negative price ({amount}); this is not a rate we can print")
    return amount, str(money.get("currencyCode") or "")


# ------------------------------------------------------------------------ pricer

def price(resource: model.Resource,
          rates: Rates) -> tuple[model.Money | None, model.Unpriced | None]:
    """Exactly one of (Money, Unpriced). A refusal is a state plus a short reason, never a zero:
    `UNSIZED` = no measurement, `UNPRICED` = no honest rate, `REFUSED` = the read failed."""
    try:
        return _price(resource, rates), None
    except _Declined as declined:
        return None, model.Unpriced(declined.state, declined.reason)


def _price(resource: model.Resource, rates: Rates) -> model.Money:
    sheet = KIND_SHEET.get(resource.kind)
    if sheet is None:
        raise _Declined(model.UNPRICED,
                        f"kind {resource.kind} has no price table in v1")
    if sheet in rates.unavailable:
        raise _Declined(model.REFUSED, f"price sheet unavailable: {rates.unavailable[sheet]}")
    handler = {"Instance": _instance, "Disk": _disk, "Snapshot": _snapshot, "Image": _image,
               "Address": _address, "Bucket": _bucket, "Table": _table}[resource.kind]
    return handler(resource, rates)


@dataclass(frozen=True)
class _Where:
    """A location: `said` is the API's string, `region` the single region it prices in (empty for
    an area, `global` or a dual-region). Refusals print `named`, the API's spelling where possible."""

    said: str
    region: str

    @property
    def named(self) -> str:
        return (self.said if not self.region or self.said.casefold() == self.region.casefold()
                else self.region)


def _where(location: str) -> _Where:
    """A zone names its region and a region names itself, in either case. Areas, `global` and
    dual-regions get an empty `region`: folding `US` to `us` would price a multi-region bucket on
    whichever record answered first. `_bucket` decides whether an area routes to its own record."""
    said = (location or "").strip().rstrip("/").rsplit("/", 1)[-1]
    zone = _ZONE.match(said)
    return _Where(said=said,
                  region=zone.group(1) if zone else (said if _SINGLE_REGION.match(said) else ""))


def _basename(value: str) -> str:
    """The last segment of a URL, or the value itself: a class name may arrive either way."""
    return (value or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _money(rate: Rate, amount: Decimal, unit: str) -> model.Money:
    return model.Money(usd_per_month=amount, sku=rate.sku_id, as_of=rate.as_of,
                       currency=rate.currency or "USD", unit=unit)


def _machine_rate(rates: Rates, phrase: str, unit: str, where: _Where) -> Rate:
    """One machine-family record: Compute family, one description head, one unit, one place."""
    return rates.one(sheet=SHEET_COMPUTE, family="Compute", group="", phrase=phrase, unit=unit,
                     region=where.region, where=where.named, tail=_TAIL_RUNNING_IN)


def _memory_split(*, family: str, cpus: int, memory_mb: int, custom: bool,
                  extended: bool) -> tuple[Decimal, Decimal]:
    """(ordinary MB, extended MB): `base = min(total, vcpus × ceiling)`, the rest extended.

    Predefined shapes are one band (the ceiling governs custom shapes only). A custom shape refuses
    on `-ext` with no ceiling on file, or on RAM above the ceiling without `-ext`: either way the
    excess's rate is unknown. A custom family with no ceiling and no `-ext` is one band.
    """
    if not custom:
        return Decimal(memory_mb), Decimal(0)
    ceiling = RAM_CEILINGS.get(family)
    if ceiling is None:
        if not extended:
            return Decimal(memory_mb), Decimal(0)
        known = ", ".join(sorted(RAM_CEILINGS))
        raise _Declined(model.UNPRICED,
                        f"no per-vCPU memory ceiling on file for the {family} family, so its "
                        f"extended memory cannot be split (ceilings on file: {known})")
    limit = ceiling.gib_per_vcpu * MB_PER_GIB * Decimal(cpus)
    base = min(Decimal(memory_mb), limit)
    excess = Decimal(memory_mb) - base
    if excess and not extended:
        raise _Declined(model.UNPRICED,
                        f"{memory_mb} MB on {cpus} vCPUs is above the {family} family's "
                        f"{ceiling.gib_per_vcpu} GB-per-vCPU ceiling and the name does not carry "
                        f"{EXTENDED_MARKER}, so which rate the excess bills at is unobserved")
    return base, excess


def _unmodelled_vm_charges(resource: model.Resource) -> list[str]:
    """What this VM bills that the core + RAM + GPU price would get wrong. Any one makes the row
    UNPRICED: a partial price is worse than none."""
    extra = resource.extra
    found = []
    provisioning = extra.get("provisioning_model")
    if provisioning and provisioning not in SPOT_MODELS:
        found.append(f"{provisioning} provisioning bills at rates not priced here")
    if extra.get("local_ssd"):
        found.append(f"{extra['local_ssd']} local SSD(s) not priced here")
    if extra.get("shared_cpu"):
        found.append("shared-core machine type bills a fractional vCPU, not priced here")
    return found


def _gpu_rate(resource: model.Resource, rates: Rates, where: _Where, spot: bool) -> Rate | None:
    """The one GPU record this VM's accelerators bill on, or None when it has none."""
    if not resource.extra.get("gpus"):
        return None
    kinds = [k for k in str(resource.extra.get("gpu_type") or "").split(model.EXTRA_LIST_SEP) if k]
    if len(kinds) != 1:
        raise _Declined(model.UNPRICED, f"GPU types {kinds or [model.UNKNOWN]}: one type per VM is priced here")
    head = GPU_MODELS.get(kinds[0])
    if head is None:
        known = ", ".join(sorted(GPU_MODELS))
        raise _Declined(model.UNPRICED, f"GPU {kinds[0]} not priced here (priced: {known})")
    phrase = head + (GPU_SPOT_SUFFIX if spot else "")
    return rates.one(sheet=SHEET_COMPUTE, family="", group="", phrase=phrase, unit=UNIT_HOUR,
                     region=where.region, where=where.named, tail=_TAIL_RUNNING_IN)


def _instance(resource: model.Resource, rates: Rates) -> model.Money:
    """`cpus × core rate + RAM at its band's rate`, hourly × `HOURS_PER_MONTH`.

    `-custom-` names use the family's `Custom …` records; `-ext` adds an extended-RAM record for RAM
    above the ceiling. Any missing record refuses the whole row: half a price is worse than none.
    `as_of` is the oldest rate used and `sku` names every one.
    """
    machine = _basename(resource.extra.get("machine_type", ""))
    family = machine.split("-")[0]
    if resource.cpus is None:
        raise _Declined(model.UNSIZED, "instance cpus unobserved")
    if resource.memory_mb is None:
        raise _Declined(model.UNSIZED, "instance memoryMb unobserved")
    unmodelled = _unmodelled_vm_charges(resource)
    if unmodelled:
        raise _Declined(model.UNPRICED, "; ".join(unmodelled))
    spot = resource.extra.get("provisioning_model") in SPOT_MODELS
    table = SPOT_FAMILIES if spot else MACHINE_FAMILIES
    terms = table.get(family)
    if terms is None:
        known = ", ".join(sorted(table))
        raise _Declined(model.UNPRICED,
                        f"no dated {family or 'machine-type'} rate: family not priced here "
                        f"(priced: {known})")
    custom = CUSTOM_MARKER in machine
    if machine.endswith(EXTENDED_MARKER) and not custom:
        raise _Declined(model.UNPRICED,
                        f"{machine}: {EXTENDED_MARKER.strip('-')} memory is priced here only on a "
                        "custom machine type, and this name is not one")
    if custom and not terms.custom_core:
        raise _Declined(model.UNPRICED, f"no Spot custom-machine record is listed for {family}")
    where = _where(resource.location)
    core = _machine_rate(rates, terms.custom_core if custom else terms.core, UNIT_HOUR, where)
    used = [core]
    base_mb, extended_mb = _memory_split(
        family=family, cpus=resource.cpus, memory_mb=resource.memory_mb, custom=custom,
        extended=machine.endswith(EXTENDED_MARKER))
    ram = _machine_rate(rates, terms.custom_ram if custom else terms.ram, UNIT_GIB_HOUR, where)
    used.append(ram)
    if core.currency != ram.currency:
        raise _Declined(model.UNPRICED,
                        f"core rate is {core.currency or 'unknown'} and ram rate is "
                        f"{ram.currency or 'unknown'}, nothing here converts between them")
    amount = (core.marginal(Decimal(resource.cpus) * HOURS_PER_MONTH)
              + ram.marginal(base_mb / MB_PER_GIB * HOURS_PER_MONTH))
    if extended_mb:
        if not terms.custom_extended:
            raise _Declined(model.UNPRICED,
                            f"no extended-memory price record has been read for the {family} family, "
                            "so its extended RAM cannot be priced and no description is guessed for it")
        extended = _machine_rate(rates, terms.custom_extended, UNIT_GIB_HOUR, where)
        if extended.currency != ram.currency:
            raise _Declined(model.UNPRICED,
                            f"ram rate is {ram.currency or 'unknown'} and extended memory rate is "
                            f"{extended.currency or 'unknown'}, nothing here converts between them")
        used.append(extended)
        amount += extended.marginal(extended_mb / MB_PER_GIB * HOURS_PER_MONTH)
    gpu = _gpu_rate(resource, rates, where, spot)
    if gpu is not None:
        if gpu.currency != core.currency:
            raise _Declined(model.UNPRICED,
                            f"core rate is {core.currency or 'unknown'} and GPU rate is "
                            f"{gpu.currency or 'unknown'}, nothing here converts between them")
        used.append(gpu)
        amount += gpu.marginal(Decimal(int(resource.extra["gpus"])) * HOURS_PER_MONTH)
    return model.Money(usd_per_month=amount,
                       sku="+".join(sorted(rate.sku_id for rate in used)),
                       as_of=min(rate.as_of for rate in used),
                       currency=core.currency or "USD",
                       unit=f"{UNIT_HOUR}+{UNIT_GIB_HOUR}{HOURS_TOKEN}")


def _address(resource: model.Resource, rates: Rates) -> model.Money:
    """An unattached (RESERVED) external IPv4 address at `Static Ip Charge` × `HOURS_PER_MONTH`.
    An address in use is billed as an external IP of the resource using it, which is not modelled."""
    if resource.state != "RESERVED":
        raise _Declined(model.UNPRICED,
                        "in use: billed as an external IP of the resource using it, not priced here"
                        if resource.state == "IN_USE" else
                        f"address status {resource.state or 'unobserved'} is not priced here")
    if resource.extra.get("ip_version") == "IPV6":
        raise _Declined(model.UNPRICED, "IPv6 address ranges are not priced here")
    where = _where(resource.location)
    rate = rates.one(sheet=SHEET_COMPUTE, family="Network", group="IpAddress",
                     phrase=STATIC_IP_PHRASE, unit=UNIT_HOUR, region=where.region,
                     where=where.named)
    return _money(rate, rate.marginal(HOURS_PER_MONTH), f"{UNIT_HOUR}{HOURS_TOKEN}")


def _disk(resource: model.Resource, rates: Rates) -> model.Money:
    """`size_gb_api × the class's per-GB-month rate`. `disks.sizeGb` is already the billing unit,
    so no GiB conversion applies; an unobserved `sizeGb` is `UNSIZED`."""
    if resource.size_gb_api is None:
        raise _Declined(model.UNSIZED, "disk sizeGb unobserved")
    disk_class = _basename(resource.extra.get("storage_class", ""))
    if not disk_class:
        raise _Declined(model.UNPRICED, "disk class not observed")
    if disk_class not in PD_CLASSES:
        known = ", ".join(sorted(PD_CLASSES))
        raise _Declined(model.UNPRICED,
                        f"no dated {disk_class} rate: class not priced here (priced: {known})")
    group, phrase = PD_CLASSES[disk_class]
    where = _where(resource.location)
    if where.region and where.region == where.said:
        # A region rather than a zone means a regional disk: a separate SKU, and replicaZones is
        # not read.
        raise _Declined(model.UNPRICED,
                        f"regional disk in {where.said}: its rate is a separate SKU at "
                        "2x the zonal one and replicaZones was not read")
    rate = rates.one(sheet=SHEET_COMPUTE, family="Storage", group=group, phrase=phrase,
                     unit=UNIT_GIB_MONTH, region=where.region, where=where.named)
    return _money(rate, rate.marginal(Decimal(resource.size_gb_api)), rate.usage_unit)


def _snapshot(resource: model.Resource, rates: Rates) -> model.Money:
    """`storageBytes` in GiB (never `diskSizeGb`, which overstates by the dedup).

    A standard snapshot's row location is `global`, so the rate table is named by
    `Snapshot.storageLocations`, asked for verbatim (region or area, not through `_where`). More
    than one place refuses. With none, a regional row location is used; otherwise it refuses,
    saying whether the field was absent or empty.
    """
    if resource.size_bytes is None:
        raise _Declined(model.UNSIZED, "snapshot storageBytes unobserved")
    places = _places(resource)
    if len(places) > 1:
        raise _Declined(model.UNPRICED,
                        f"Snapshot.storageLocations named {len(places)} places "
                        f"({', '.join(places)}), and picking one is picking a bill")
    if places:
        region = named = places[0]
    else:
        where = _where(resource.location)
        if not where.region:
            # The status key is present only when the array arrived empty; that tells the two
            # silences apart.
            missing = ("Snapshot.storageLocations arrived naming nothing"
                       if resource.extra.get("storage_locations_status")
                       else "the reply carried no Snapshot.storageLocations")
            raise _Declined(model.UNPRICED,
                            f"snapshot storage place unobserved: {where.said or 'no location'!r} "
                            f"names no rate table and {missing}")
        region, named = where.region, where.named
    rate = rates.one(sheet=SHEET_COMPUTE, family="Storage", group=SNAPSHOT_GROUP,
                     phrase=SNAPSHOT_PHRASE, unit=UNIT_GIB_MONTH, region=region, where=named)
    return _money(rate, rate.marginal(_gib(resource.size_bytes)), rate.usage_unit)


def _places(resource: model.Resource) -> tuple[str, ...]:
    """`Snapshot.storageLocations` as the collector recorded it, unmodified: a stray space matches
    no record, which is a refusal rather than a guess."""
    joined = resource.extra.get("storage_locations")
    return tuple(joined.split(model.EXTRA_LIST_SEP)) if joined else ()


def _image(resource: model.Resource, rates: Rates) -> model.Money:
    """Custom images bill on `images.storageBytes`, looked up as `IMAGE_PHRASE`; absent, the row is
    `UNPRICED` rather than borrowing the snapshot rate."""
    if resource.size_bytes is None:
        raise _Declined(model.UNSIZED, "image storageBytes unobserved")
    where = _where(resource.location)
    rate = rates.one(sheet=SHEET_COMPUTE, family="Storage", group="", phrase=IMAGE_PHRASE,
                     unit=UNIT_GIB_MONTH, region=where.region, where=where.named)
    return _money(rate, rate.marginal(_gib(resource.size_bytes)), rate.usage_unit)


#: `collect_storage`'s `extra["objects_probe"]` tokens -> the `UNSIZED` reason each prints.
#: `tests/test_collect_storage.py` pins that both modules use the same set.
OBJECT_PROBE_REASONS: dict[str, str] = {
    "occupied": "the bucket holds objects and the byte gauge was silent, so its bytes are unobserved",
    "unfinished": "the object listing answered an empty page without finishing it, so a zero here "
                  "would be an invented one",
    "versioned": "versioning is enabled, so a listing of live objects cannot settle these bytes and "
                 "the gauge was silent",
    "refused": "the object listing was refused, so the bucket's bytes are unobserved",
}


def _bucket(resource: model.Resource, rates: Rates) -> model.Money:
    """Bytes from the `storage/total_bytes` gauge, priced in GiB at the class's capacity rate.

    Unsized buckets are `UNSIZED`, never zero, with the probe's or gauge's reason. An area location
    is priced on the area's own record (`AREA_PLACEMENTS`), never a folded regional one; a class
    that is regional by definition refuses an area.
    """
    if resource.size_bytes is None:
        probe = resource.extra.get("objects_probe") or ""
        raise _Declined(model.UNSIZED,
                        OBJECT_PROBE_REASONS.get(probe)
                        or resource.extra.get("not_measured")
                        or "bucket size unobserved")
    storage_class = (resource.extra.get("storage_class") or "").strip().upper()
    if not storage_class:
        raise _Declined(model.UNPRICED, "storage class not observed")
    if storage_class not in GCS_CLASSES:
        known = ", ".join(sorted(GCS_CLASSES))
        raise _Declined(model.UNPRICED,
                        f"no dated {storage_class} rate: class not priced here (priced: {known})")
    group, phrase, is_variant_class = GCS_CLASSES[storage_class]
    where = _where(resource.location)
    region, variants, label = where.region, is_variant_class, f"{storage_class.lower()} storage"
    area = AREA_PLACEMENTS.get(where.named.strip().upper()) if not region else ""
    if area and storage_class in AREA_CLASSES:
        # "Multi-region" is the area record's own name, not a variant; a second hit still refuses.
        region, variants, label = area, True, label + " area"
        group = AREA_GROUPS.get(storage_class, group)
    elif area:
        raise _Declined(model.UNPRICED,
                        f"{storage_class} is a regional placement and {where.named} names an area, "
                        "so no single record answers the pair")
    rate = rates.one(sheet=SHEET_STORAGE, family="Storage", group=group, phrase=phrase,
                     unit=UNIT_GIB_MONTH, region=region, where=where.named,
                     tail=_TAIL_PLACE, label=label, match_variants=variants)
    return _money(rate, rate.marginal(_gib(resource.size_bytes)), rate.usage_unit)


#: Reasons for a Table whose active/long-term split did not settle.
SPLIT_UNOBSERVED = "the active/long-term byte split was not answered by the read that sized it"
SPLIT_DISAGREED = "the byte counts disagree, so no tier was taken"


def _table(resource: model.Resource, rates: Rates) -> model.Money:
    """Active and long-term logical bytes, each on its own dated record, summed.

    The split comes from `bigquery.tables.get` (`docs/API-EVIDENCE.md`); halves that disagree are
    refused. An area dataset prices on the area's own pair, never a folded single region (whose rate
    differs, e.g. 0.023 vs 0.02). The free 10 GiB tier is applied per row; whether Google applies it
    per table or per project is an open question (`docs/DESIGN.md`).
    """
    if resource.size_bytes is None:
        raise _Declined(model.UNSIZED,
                        resource.extra.get("not_measured") or "table size unobserved")
    disagree = resource.extra.get("split_disagrees")
    if disagree:
        raise _Declined(model.REFUSED, f"{SPLIT_DISAGREED}: {disagree}")
    active = resource.extra.get("active_logical_bytes")
    long_term = resource.extra.get("long_term_logical_bytes")
    if active is None or long_term is None:
        raise _Declined(model.UNPRICED, SPLIT_UNOBSERVED)
    where = _where(resource.location)
    region = where.region or AREA_PLACEMENTS.get(where.said.strip().upper(), "")
    if not region:
        raise _Declined(model.UNPRICED,
                        f"{where.said} is neither one region nor a known area, so no record "
                        "answers the pair")
    wanted = (("ActiveStorage", "Active Logical Storage", Decimal(int(active))),
              ("LongTermStorage", "Long Term Logical Storage", Decimal(int(long_term))))
    used: list[Rate] = []
    amount = Decimal(0)
    # Skip a zero half so a missing record for it cannot hide the other half's price. If both are
    # zero, one record is still looked up so a `0.00` row cites a dated price.
    billed_somewhere = any(quantity != 0 for _group, _phrase, quantity in wanted)
    for group, phrase, quantity in wanted:
        if quantity == 0 and (billed_somewhere or used):
            continue
        rate = rates.one(sheet=SHEET_BIGQUERY, family="ApplicationServices", group=group,
                         phrase=phrase, unit=UNIT_GIB_MONTH, region=region, where=where.said,
                         tail=_TAIL_PLACE, label=phrase.lower())
        if used and rate.currency and used[0].currency and rate.currency != used[0].currency:
            raise _Declined(model.UNPRICED,
                            f"the active rate is {used[0].currency} and the long-term rate is "
                            f"{rate.currency}, nothing here converts between them")
        used.append(rate)
        amount += rate.marginal(_gib(int(quantity)))
    return model.Money(usd_per_month=amount,
                       sku="+".join(sorted(rate.sku_id for rate in used)),
                       as_of=min(rate.as_of for rate in used),
                       currency=used[0].currency or "USD",
                       unit=UNIT_GIB_MONTH)


def _gib(size_bytes: int) -> Decimal:
    """Bytes -> GiB (`model.GIB` = 2**30), `Decimal`. The only bytes-to-GiB division in the package."""
    return Decimal(size_bytes) / Decimal(model.GIB)
