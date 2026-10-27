"""The price sheet: what loads, what is dropped, and what a refusal says.

Tests stage `skus.list` payloads through `tests/fake_session.py`, so `guard` runs for real. The
payloads use the discovery document's field names: `pricingInfo[].effectiveTime`,
`pricingExpression.tieredRates[].unitPrice{units,nanos}`, `serviceRegions`,
`category{resourceFamily,resourceGroup,usageType}` and `description`. Most tests check the rate's
date and its scale, the two facts most likely to make a cell wrong.
"""
from __future__ import annotations

import ast
import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fake_session import install

from gcp_audit import guard, model, pricing

AS_OF = "2026-10-01"
COMPUTE_ID = "services/6F81-5844-456A"
STORAGE_ID = "services/95FF-2EF5-5EA1"

#: `Service.serviceId` is the bare identifier; `Service.name` is the resource name
#: `services/<id>`. The cloudbilling v1 schema examples show both forms. Both are staged so a test
#: can catch code that confuses them.
COMPUTE_SERVICE_ID = "6F81-5844-456A"
STORAGE_SERVICE_ID = "95FF-2EF5-5EA1"

#: `Service` in cloudbilling v1 is `{serviceId, name, displayName, businessEntityName}`.
#: The service list holds several names that contain "BigQuery" besides BigQuery itself. The
#: staged decoy, `superQuery for BigQuery`, catches a substring match, which would fetch and use
#: the wrong rate card.
BIGQUERY_SERVICE_ID = "24E6-581D-38E5"
BIGQUERY_ID = "services/24E6-581D-38E5"

SERVICES = {"services": [
    {"serviceId": COMPUTE_SERVICE_ID, "name": COMPUTE_ID,
     "displayName": "Compute Engine",
     "category": {"serviceGroups": ["Compute"]}},
    {"serviceId": STORAGE_SERVICE_ID, "name": STORAGE_ID,
     "displayName": "Cloud Storage",
     "category": {"serviceGroups": ["Storage"]}},
    {"serviceId": BIGQUERY_SERVICE_ID, "name": BIGQUERY_ID, "displayName": "BigQuery"},
    {"serviceId": "F33E-369C-A8B4", "name": "services/F33E-369C-A8B4",
     "displayName": "superQuery for BigQuery"},
]}

#: The same two services, named only by `serviceDisplayName`. `Service` has no such field, so
#: this payload must load no sheet.
SERVICES_WRONG_FIELD = {"services": [
    {"serviceId": COMPUTE_SERVICE_ID, "serviceDisplayName": "Compute Engine"},
    {"serviceId": STORAGE_SERVICE_ID, "serviceDisplayName": "Cloud Storage"},
]}


def money(units: str, nanos: int, currency: str = "USD") -> dict[str, object]:
    """A `Money` message as the API sends it: whole `units` as an int64 string, `nanos` as an int."""
    return {"currencyCode": currency, "units": units, "nanos": nanos}


def sku(sku_id: str, description: str, *, group: str = "", family: str = "Storage",
        unit: str = "GiBy.mo", regions: tuple[str, ...] = ("us-central1",),
        effective: str = "2026-01-01T00:00:00Z",
        tiers: list[tuple[object, dict]] | None = None,
        pricing_period: dict | None = None, geo: dict | None = None) -> dict:
    """One `Sku` payload, discovery field names, one `pricingInfo` entry unless told otherwise."""
    record = {
        "name": f"{COMPUTE_ID}/skus/{sku_id}", "skuId": sku_id, "description": description,
        "category": {"resourceFamily": family, "resourceGroup": group,
                     "serviceDisplayName": "Compute Engine", "usageType": "OnDemand"},
        "serviceProviderName": "Google", "serviceRegions": list(regions),
        "pricingInfo": [{
            "effectiveTime": effective, "summary": "",
            "pricingExpression": {
                "displayQuantity": 1, "usageUnit": unit,
                "tieredRates": [{"startUsageAmount": start, "unitPrice": price}
                                for start, price in (tiers or [("", money("0", 0))])],
            },
        }],
    }
    if geo is not None:
        record["geoTaxonomy"] = geo
    if pricing_period is not None:
        record["pricingPeriod"] = pricing_period
    return record


def when(days: int) -> str:
    """A date `days` from today, for the tests that ask what `load_rates()` does with no as_of."""
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(compute_records: list[dict], storage_records: list[dict],
         bigquery_records: list[dict] | None = None, as_of: str = AS_OF,
         routes: dict | None = None) -> pricing.Rates:
    """Stage the three sheets and load them.

    `skus.list` is called once per sheet in `NEEDED_SHEETS` order: Compute Engine, Cloud Storage,
    then BigQuery. With no BigQuery records the BigQuery sheet is empty rather than missing, so a
    BigQuery row fails for lack of a rate, not for lack of a route."""
    install(routes=routes or {
        "cloudbilling.services.list": SERVICES,
        "cloudbilling.services.skus.list": [{"skus": compute_records},
                                            {"skus": storage_records},
                                            {"skus": bigquery_records or []}],
    })
    return pricing.load_rates(as_of)


PD_STANDARD = sku("3PDS-74D4-57D4", "Storage PD Capacity", group="PDStandard",
                  tiers=[(0, money("0", 0)), (30, money("0", 40000000))],
                  regions=("us-central1", "us-east1", "us-west1"))
PD_SSD = sku("SSD1-9A11-0001", "SSD backed PD Capacity", group="SSD",
             tiers=[(0, money("0", 170000000))])
# Two snapshot records: one for a regional snapshot, one for a multi-region (area) snapshot.
#
# `SNAPSHOT_REGIONAL` has a real description, regions and $0.05 rate, but a synthetic skuId. No
# real id for a `Storage PD Snapshot` record without a place suffix is on record.
#
# `SNAPSHOT_AREA` is a real record: `34CF-7D88-5D40` "Storage PD Snapshot in US",
# `resourceGroup: PDSnapshot`, one tier at 0.065 USD per `GiBy.mo`, offered at `us`
# (`docs/API-EVIDENCE.md` §Snapshots and images). Its real `effectiveTime`, 2026-10-13, is after
# `AS_OF`, so the fixture uses the default date instead.
#
# The rates differ on purpose: a row priced at 0.065 used the area record, not the region one.
SNAPSHOT_REGIONAL = sku("SYNTH-SNAP-REG-01", "Storage PD Snapshot", group="PDSnapshot",
                        tiers=[(0, money("0", 50000000))],
                        regions=("us-central1", "us-east1", "us-west1", "asia-east1"))
SNAPSHOT_AREA = sku("34CF-7D88-5D40", "Storage PD Snapshot in US", group="PDSnapshot",
                    tiers=[(0, money("0", 65000000))], regions=("us",))
CORE_N1 = sku("2E27-4F75-95CD", "N1 Predefined Instance Core running in Americas",
              family="Compute", unit="h", tiers=[(0, money("0", 31611000))],
              regions=("us-central1", "us-east1", "us-west1"))
RAM_N1 = sku("6C71-E844-38BC", "N1 Predefined Instance Ram running in Americas",
             family="Compute", unit="GiBy.h", tiers=[(0, money("0", 4237000))],
             regions=("us-central1", "us-east1", "us-west1"))
STANDARD_FLAT = sku("E5F0-6A5D-7BAD", "Standard Storage US Regional", group="RegionalStorage",
                    tiers=[(0, money("0", 20000000))],
                    regions=("us-west1", "us-central1", "us-east1"))

# Three Cloud Storage area records from the 2026-10-19 capture of `services/95FF-2EF5-5EA1`
# (`docs/API-EVIDENCE.md` §Twelve Cloud Storage capacity records offered at an area). Id,
# description, `resourceGroup`, region and rate are real. `effectiveTime` is the fixture default
# because the real date is after `AS_OF`. `0D5D-6E23-4250` costs 0.026 where the regional
# `E5F0-6A5D-7BAD` costs 0.02, so a row priced at 0.026 used the area record.
STANDARD_US_MULTI = sku("0D5D-6E23-4250", "Standard Storage US Multi-region",
                        group="MultiRegionalStorage", tiers=[(0, money("0", 26000000))],
                        regions=("us",))
NEARLINE_EU_MULTI = sku("4CF0-069A-15D9", "Nearline Storage Europe Multi-region",
                        group="NearlineStorage", tiers=[(0, money("0", 15000000))],
                        regions=("europe",))
ARCHIVE_ASIA_MULTI = sku("4B5C-6444-A206", "Archive Storage Asia Multi-region",
                         group="ArchiveStorage", tiers=[(0, money("0", 3000000))],
                         regions=("asia",))


def resource(kind: str, name: str = "thing", **fields: object) -> model.Resource:
    return model.Resource(project="lab-seq-prd", service="compute.googleapis.com", kind=kind,
                          name=name, **fields)


def price(res: model.Resource, rates: pricing.Rates) -> tuple[model.Money | None,
                                                              model.Unpriced | None]:
    return pricing.price(res, rates)


# --------------------------------------------------------------------- the loader

def test_the_price_sheet_is_two_reads_paged_in_full_with_no_server_side_filter():
    """`skus.list` has no filter parameter, so the whole sheet is paged and filtered locally.
    Passing `startTime` would return a slice, and a rate outside it would look like no rate."""
    fake = install(routes={
        "cloudbilling.services.list": [{"services": [SERVICES["services"][0]],
                                        "nextPageToken": "SVCTOK"},
                                       {"services": [SERVICES["services"][1]]}],
        "cloudbilling.services.skus.list": [{"skus": [PD_SSD], "nextPageToken": "SKUTOK"},
                                            {"skus": []},
                                            {"skus": [STANDARD_FLAT]}],
    })
    rates = pricing.load_rates(AS_OF)
    assert fake.count("cloudbilling.services.list") == 2
    assert fake.queries("cloudbilling.services.list")[1]["pageToken"] == "SVCTOK"
    assert fake.queries("cloudbilling.services.skus.list")[1]["pageToken"] == "SKUTOK"
    assert fake.urls("cloudbilling.services.skus.list") == [
        f"https://cloudbilling.googleapis.com/v1/{COMPUTE_ID}/skus",
        f"https://cloudbilling.googleapis.com/v1/{COMPUTE_ID}/skus",
        f"https://cloudbilling.googleapis.com/v1/{STORAGE_ID}/skus"]
    for query in fake.queries("cloudbilling.services.skus.list"):
        # Pagination only: no startTime, endTime or currencyCode.
        assert query["pageSize"] == pricing.PAGE_SIZE
        assert set(query) <= {"pageSize", "pageToken"}
    assert [rate.sku_id for rate in rates.rates] == ["SSD1-9A11-0001", "E5F0-6A5D-7BAD"]


def test_a_bare_serviceId_still_reaches_the_sheet_because_that_is_what_the_api_returns():
    """`Service.serviceId` is the bare id, `6F81-5844-456A`. `skus.list` is addressed at
    `services/6F81-5844-456A`. A service that carries only the bare id must still load."""
    fake = install(routes={
        "cloudbilling.services.list": {"services": [
            {"serviceId": COMPUTE_SERVICE_ID,
             "businessEntityName": "billingServices/013C-8985-2DC4",
             "displayName": "Compute Engine"}]},
        "cloudbilling.services.skus.list": {"skus": [PD_SSD]},
    })
    rates = pricing.load_rates(AS_OF)
    assert fake.count("cloudbilling.services.skus.list") == 1
    assert fake.urls("cloudbilling.services.skus.list") == [
        f"https://cloudbilling.googleapis.com/v1/{COMPUTE_ID}/skus"]
    assert pricing.SHEET_COMPUTE not in rates.unavailable
    assert [rate.sku_id for rate in rates.rates] == ["SSD1-9A11-0001"]
    assert rates.rejected == []


def test_an_identifier_that_cannot_be_addressed_names_itself_rather_than_becoming_missing():
    """An identifier that cannot become `services/<id>` is rejected by name. The gap must not say
    "did not list it", because the service was listed. A false reason sends the operator after the
    wrong fix."""
    fake = install(routes={
        "cloudbilling.services.list": {"services": [
            {"serviceId": "Compute Engine", "displayName": "Compute Engine"}]},
        "cloudbilling.services.skus.list": {"skus": []},
    })
    rates = pricing.load_rates(AS_OF)
    assert rates.rates == []
    assert fake.count("cloudbilling.services.skus.list") == 0
    assert any("Compute Engine" in identifier for identifier, _ in rates.rejected)
    assert "did not list it" not in rates.unavailable.get(pricing.SHEET_COMPUTE, "")


def test_a_sku_expired_at_as_of_is_dropped_not_printed_with_a_stale_date():
    """An expired rate is a wrong number, not an old price. Only the entry in force at `as_of`
    loads, and a date before every entry loads nothing."""
    # Synthetic id: the dates and second rate are made up, so they must not use a real record's id.
    record = sku("SYNTH-SNAP-TL-01", "Storage PD Snapshot", group="PDSnapshot",
                 tiers=[(0, money("0", 65000000))], effective="2025-01-01T00:00:00Z")
    record["pricingInfo"].append({
        "effectiveTime": "2026-06-01T00:00:00Z",
        "pricingExpression": {"usageUnit": "GiBy.mo",
                              "tieredRates": [{"startUsageAmount": 0,
                                               "unitPrice": money("0", 70000000)}]},
    })
    assert [rate.as_of for rate in load([record], []).rates] == ["2026-06-01"]
    assert [rate.as_of for rate in load([record], [], as_of="2025-06-01").rates] == ["2025-01-01"]
    expired = load([record], [], as_of="2024-01-01")
    assert expired.rates == []
    _money_out, refusal = price(
        resource("Snapshot", "eren-gpu-20261001", location="us-central1", size_bytes=2 ** 30),
        expired)
    assert _money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == "no dated Storage PD Snapshot rate for us-central1"


def test_a_record_that_carries_no_date_at_all_is_refused_rather_than_assumed_current():
    """With no `effectiveTime` and no `pricingPeriod` the rate has no date, so it cannot supply
    `price_as_of`. It is rejected and counted."""
    undated = sku("NO-DATE-0001", "Storage PD Snapshot", group="PDSnapshot",
                  tiers=[(0, money("0", 65000000))])
    undated["pricingInfo"][0].pop("effectiveTime")
    rates = load([undated], [])
    assert rates.rates == []
    assert rates.rejected == [("NO-DATE-0001", "pricingInfo.effectiveTime is absent or unparsable "
                                               "(None), so the price is undated")]


def test_an_explicit_pricing_period_is_honoured_when_a_payload_carries_one():
    """A cached catalog stamps the range it was fetched for. That range wins over the
    `effectiveTime` chain, and `endTime` is exclusive."""
    record = sku("CACHE-0001-0001", "Storage PD Snapshot", group="PDSnapshot",
                 tiers=[(0, money("0", 65000000))],
                 pricing_period={"startTime": "2026-09-01T00:00:00Z",
                                 "endTime": "2026-10-01T00:00:00Z"})
    assert [rate.as_of for rate in load([record], [], as_of="2026-09-15").rates] == ["2026-09-01"]
    assert not load([record], [], as_of="2026-10-01").rates


def test_a_bare_number_in_a_price_field_is_refused_because_it_declares_no_scale():
    """`0.02` does not say whether it means dollars, cents or nanos. The record is rejected at load
    and counted, and the row prints `NA`."""
    float_units = sku("BARE-0001-0001", "Storage PD Snapshot", group="PDSnapshot",
                      tiers=[(0, {"currencyCode": "USD", "units": 0.02})])
    bare_number = sku("BARE-0002-0002", "Storage PD Capacity", group="PDStandard",
                      tiers=[(0, 0.02)])
    rates = load([float_units, bare_number], [])
    assert rates.rates == []
    assert [reason for _sku, reason in rates.rejected] == [
        "BARE-0001-0001 tier 0.unitPrice has units=0.02, which is not an integer units — "
        "a fractional number declares no scale",
        "BARE-0002-0002 tier 0.unitPrice is float (0.02); a bare number declares no scale — "
        "it must carry units and nanos",
    ]
    snapshot = resource("Snapshot", "nightly", location="us-central1", size_bytes=2 ** 30)
    money_out, refusal = price(snapshot, rates)
    assert money_out is None
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated Storage PD Snapshot rate for us-central1"
    assert model.Row.of(snapshot, money_out, refusal).evidence["reason"] == refusal.reason


def test_a_failed_price_read_names_itself_rather_than_becoming_no_rates():
    install(routes={"cloudbilling.services.list": SERVICES,
                    "cloudbilling.services.skus.list": [{"skus": [PD_SSD]}]},
            failures={"cloudbilling.services.skus.list": (403, "PERMISSION_DENIED cloud-billing")})
    rates = pricing.load_rates(AS_OF)
    assert rates.rates == []                  # a half-read sheet is dropped, not kept
    assert rates.unavailable == {sheet: "PERMISSION_DENIED cloud-billing"
                                 for sheet in pricing.NEEDED_SHEETS}


def test_a_service_named_only_by_the_field_that_does_not_exist_finds_no_sheet():
    """`serviceDisplayName` belongs to a `Sku`'s `Category`, not to `Service`. A service named only
    by that field loads no sheet, and the gap says the catalog did not list it."""
    install(routes={"cloudbilling.services.list": SERVICES_WRONG_FIELD,
                    "cloudbilling.services.skus.list": [{"skus": [PD_SSD]}, {"skus": []}]})
    rates = pricing.load_rates(AS_OF)
    assert rates.rates == []
    assert set(rates.unavailable) == set(pricing.NEEDED_SHEETS)
    assert "did not list it" in rates.unavailable[pricing.SHEET_COMPUTE]


def test_no_price_sheet_at_all_refuses_every_row_with_the_reason():
    """With no price sheet every row is `REFUSED` with the read's error. `UNPRICED` would wrongly
    claim Google has no rate, when the tool simply could not read one."""
    install(routes={}, failures={"cloudbilling.services.list":
                                 (403, "PERMISSION_DENIED cloud-billing.readonly is not granted")})
    rates = pricing.load_rates(AS_OF)
    for kind, fields in [("Instance", {"cpus": 2, "memory_mb": 2048}),
                         ("Disk", {"size_gb_api": 10}),
                         ("Snapshot", {"size_bytes": 2 ** 30}),
                         ("Image", {"size_bytes": 2 ** 30}),
                         ("Bucket", {"size_bytes": 2 ** 30})]:
        money_out, refusal = price(
            model.Resource(project="lab-seq-prd", service="compute.googleapis.com", kind=kind,
                           name="x", location="us-central1", **fields), rates)
        assert money_out is None
        assert refusal.state == model.REFUSED
        assert refusal.reason == ("price sheet unavailable: PERMISSION_DENIED "
                                  "cloud-billing.readonly is not granted")


def test_a_service_the_catalog_does_not_list_is_a_named_gap():
    load([PD_SSD], [], routes={"cloudbilling.services.list": {"services": [SERVICES["services"][0]]},
                               "cloudbilling.services.skus.list": {"skus": [PD_SSD]}})
    rates = pricing.load_rates(AS_OF)
    assert rates.unavailable == {pricing.SHEET_STORAGE: "cloudbilling.services.list did not list it",
                                 pricing.SHEET_BIGQUERY: "cloudbilling.services.list did not list it"}
    assert [rate.sku_id for rate in rates.rates] == ["SSD1-9A11-0001"]


def test_as_of_defaults_to_today_so_a_future_rate_is_not_this_months_price():
    past = sku("PAST-0001-0001", "Storage PD Snapshot", group="PDSnapshot",
               tiers=[(0, money("0", 65000000))], effective=when(-400))
    future = sku("FUTU-RE01-0001", "Storage PD Snapshot", group="PDSnapshot",
                 tiers=[(0, money("0", 65000000))], effective=when(+30))
    install(routes={"cloudbilling.services.list": SERVICES,
                    "cloudbilling.services.skus.list": [{"skus": [past, future]}, {"skus": []},
                                            {"skus": []}]})
    rates = pricing.load_rates()
    assert [rate.sku_id for rate in rates.rates] == ["PAST-0001-0001"]
    assert rates.as_of == model.date_only(model.now_utc())


def test_an_as_of_that_is_not_a_date_is_an_error_not_a_silently_empty_sheet():
    install(routes={"cloudbilling.services.list": SERVICES,
                    "cloudbilling.services.skus.list": [{"skus": []}, {"skus": []}]})
    with pytest.raises(guard.AuditError):
        pricing.load_rates("last Tuesday")


# ---------------------------------------------------------------------- instances

def test_a_dated_instance_price_is_cpus_x_core_plus_ram_x_per_gb_at_730_hours():
    """The core record is per hour and the RAM record per GiB-hour, so a month is 730 hours.
    `Money.unit` records that convention so the cell does not hide it."""
    rates = load([CORE_N1, RAM_N1], [])
    vm = resource("Instance", "seq-worker", location="us-central1-c", cpus=4, memory_mb=15360,
                  extra={"machine_type": "https://www.googleapis.com/compute/v1/projects"
                                         "/lab-seq-prd/zones/us-central1-c/machineTypes"
                                         "/n1-standard-4"})
    money_out, refusal = price(vm, rates)
    assert refusal is None
    expected = (Decimal("0.031611") * 4 * pricing.HOURS_PER_MONTH
                + Decimal("0.004237") * (Decimal(15360) / Decimal(1024))
                * pricing.HOURS_PER_MONTH)
    assert money_out.usd_per_month == expected == Decimal("138.69927")
    assert money_out.to_cell() == "138.70"
    assert money_out.sku == "2E27-4F75-95CD+6C71-E844-38BC"      # one number, two SKUs
    assert money_out.as_of == "2026-01-01"                        # the older of the two rates
    assert money_out.currency == "USD"
    assert money_out.unit == "h+GiBy.h@730h/mo"
    assert "730" in pricing.MONEY_CONVENTION


def test_an_instance_of_a_family_this_build_has_no_descriptions_for_is_unpriced():
    """`c3` may have a real rate, but the module does not know its SKU description. Guessing the
    description would mean guessing the price."""
    rates = load([CORE_N1, RAM_N1], [])
    _money_out, refusal = price(
        resource("Instance", "c3-box", location="us-central1-a", cpus=4, memory_mb=8192,
                 extra={"machine_type": "c3-standard-4"}), rates)
    assert refusal.state == model.UNPRICED
    assert "c3" in refusal.reason and "e2, n1, n2, n2d" in refusal.reason


def test_a_machine_family_with_no_dated_record_in_its_region_is_unpriced_naming_both():
    rates = load([CORE_N1, RAM_N1], [])                            # N1 only; this VM is an N2
    _money_out, refusal = price(
        resource("Instance", "n2-box", location="us-central1-a", cpus=4, memory_mb=8192,
                 extra={"machine_type": "projects/p/zones/us-central1-a/machineTypes"
                                        "/n2-standard-4"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated N2 Instance Core rate for us-central1"


def test_a_machine_record_at_the_wrong_unit_is_not_an_instance_rate():
    """`GiBy.d` and `count` are real usage units. A rate whose link to a month would have to be
    guessed is refused, not multiplied by 30."""
    daily = copy.deepcopy(CORE_N1)
    daily["pricingInfo"][0]["pricingExpression"]["usageUnit"] = "GiBy.d"
    rates = load([daily, RAM_N1], [])
    _money_out, refusal = price(
        resource("Instance", "seq-worker", location="us-central1-c", cpus=4, memory_mb=15360,
                 extra={"machine_type": "n1-standard-4"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated N1 Predefined Instance Core rate for us-central1"


def test_a_sole_tenancy_record_cannot_price_an_ordinary_vm():
    """A sole-tenancy core record exists in the same region. Matching the start of the description
    keeps it out of an N1 lookup."""
    sole = sku("SOLE-0001-0001", "Sole Tenancy N1 Predefined Instance Core running in Americas",
               family="Compute", unit="h", tiers=[(0, money("0", 99000000))])
    rates = load([CORE_N1, RAM_N1, sole], [])
    money_out, refusal = price(
        resource("Instance", "seq-worker", location="us-central1-c", cpus=4, memory_mb=15360,
                 extra={"machine_type": "n1-standard-4"}), rates)
    assert refusal is None and money_out.sku.startswith("2E27-4F75-95CD")


def test_an_instance_whose_machine_type_was_never_measured_is_UNSIZED_not_a_free_vm():
    """When the machine-type read fails, `collect_compute` leaves `cpus` and `memory_mb` as `None`
    and sets `not_measured`. The row must carry no number."""
    rates = load([CORE_N1, RAM_N1], [])
    for fields in ({"cpus": None, "memory_mb": 15360}, {"cpus": 4, "memory_mb": None}):
        _money_out, refusal = price(
            resource("Instance", "ghost", location="us-central1-c",
                     extra={"not_measured": "machine type not listed"}, **fields), rates)
        assert refusal.state == model.UNSIZED
    _money_out, refusal = price(
        resource("Instance", "ghost", location="us-central1-c", cpus=4, memory_mb=15360), rates)
    assert refusal.state == model.UNPRICED and "machine-type" in refusal.reason


def test_two_matching_rates_are_a_refusal_to_pick_one():
    """Two dated records for one family and region can happen, for example with a reissued SKU id.
    The row is refused with a reason, never priced at either rate."""
    twin = dict(RAM_N1, skuId="6C71-E844-9999", name=f"{COMPUTE_ID}/skus/6C71-E844-9999")
    rates = load([CORE_N1, RAM_N1, twin], [])
    _money_out, refusal = price(
        resource("Instance", "seq-worker", location="us-central1-c", cpus=4, memory_mb=15360,
                 extra={"machine_type": "n1-standard-4"}), rates)
    assert refusal.state == model.UNPRICED
    assert "2 dated N1 Predefined Instance Ram rates for us-central1" in refusal.reason


def test_two_halves_of_one_vm_in_two_currencies_are_not_summed():
    ram = copy.deepcopy(RAM_N1)
    ram["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"] = \
        money("0", 4237000, "EUR")
    rates = load([CORE_N1, ram], [])
    _money_out, refusal = price(
        resource("Instance", "seq-worker", location="us-central1-c", cpus=4, memory_mb=15360,
                 extra={"machine_type": "n1-standard-4"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == ("core rate is USD and ram rate is EUR, nothing here converts "
                              "between them")


# -------------------------------------------------------------------------- disks

def test_a_disk_prices_on_size_gb_api_because_that_field_is_already_the_billing_unit():
    """`disks.sizeGb` is already the billing unit, so no GiB conversion applies. The first 30 GiB of
    pd-standard are free, so a 100 GB disk is billed for 70."""
    rates = load([PD_STANDARD], [])
    disk = resource("Disk", "seq-data", location="us-central1-c", size_gb_api=100,
                    extra={"storage_class": "projects/p/zones/us-central1-c/diskTypes/pd-standard"})
    money_out, refusal = price(disk, rates)
    assert refusal is None
    assert money_out.usd_per_month == Decimal("0.04") * 70 == Decimal("2.80")
    assert money_out.sku == "3PDS-74D4-57D4" and money_out.unit == "GiBy.mo"
    assert money_out.as_of == "2026-01-01"


def test_a_disk_under_the_free_step_of_its_own_ladder_is_a_real_zero():
    rates = load([PD_STANDARD], [])
    money_out, refusal = price(
        resource("Disk", "small", location="us-central1-a", size_gb_api=20,
                 extra={"storage_class": "pd-standard"}), rates)
    assert refusal is None
    assert money_out.usd_per_month == Decimal(0)                  # a real zero, not a gap
    assert money_out.to_cell() == "0.00"


def test_a_disk_never_prices_on_bytes_because_size_gb_api_is_the_only_observed_size():
    """Converting `size_bytes` to a disk size would print a number the API never gave, in a unit
    disks are not billed in."""
    rates = load([PD_SSD], [])
    _money_out, refusal = price(
        resource("Disk", "weird", location="us-central1-a", size_bytes=100 * 2 ** 30,
                 extra={"storage_class": "pd-ssd"}), rates)
    assert refusal.state == model.UNSIZED
    assert refusal.reason == "disk sizeGb unobserved"


def test_a_disk_with_no_class_is_unpriced_because_pd_standard_and_pd_ssd_are_4x_apart():
    rates = load([PD_STANDARD, PD_SSD], [])
    _money_out, refusal = price(
        resource("Disk", "mystery", location="us-central1-a", size_gb_api=100), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "disk class not observed"


def test_a_disk_class_this_build_has_no_record_for_is_unpriced_naming_the_class():
    rates = load([PD_STANDARD], [])
    _money_out, refusal = price(
        resource("Disk", "fast", location="us-central1-a", size_gb_api=100,
                 extra={"storage_class": "hyperdisk-extreme"}), rates)
    assert refusal.state == model.UNPRICED
    assert "hyperdisk-extreme" in refusal.reason


def test_a_regional_disk_is_not_priced_off_the_zonal_rate():
    """Zonal and regional PD capacity are separate SKUs, 2x apart. `replicaZones` is not read, so a
    disk whose location is a region is refused."""
    rates = load([PD_STANDARD], [])
    _money_out, refusal = price(
        resource("Disk", "regional", location="us-central1", size_gb_api=100,
                 extra={"storage_class": "pd-standard"}), rates)
    assert refusal.state == model.UNPRICED
    assert "regional disk in us-central1" in refusal.reason


def test_the_regional_variant_record_cannot_answer_a_zonal_disk_lookup():
    """"Regional Storage PD Capacity" has the same group and region as the zonal record at 2x the
    price. Matching the start of the description keeps them apart."""
    regional = sku("REGL-STD-4444", "Regional Storage PD Capacity", group="PDStandard",
                   tiers=[(0, money("0", 80000000))])
    rates = load([PD_STANDARD, regional], [])
    money_out, refusal = price(
        resource("Disk", "seq-data", location="us-central1-c", size_gb_api=100,
                 extra={"storage_class": "pd-standard"}), rates)
    assert refusal is None
    assert money_out.usd_per_month == Decimal("2.80")             # 0.04 x 70, not 0.08 x 100


# ------------------------------------------------- snapshots, images, buckets

def test_a_snapshot_prices_on_bytes_in_gibibytes_and_never_on_the_source_disk_size():
    """`storageBytes` is the deduplicated size the bill uses. `diskSizeGb` is the source disk's
    size and overstates it. With no `storageLocations`, the snapshot's own `location` (a region
    here) chooses the rate."""
    rates = load([SNAPSHOT_REGIONAL], [])
    snapshot = resource("Snapshot", "eren-gpu-20261001", location="us-central1",
                        size_bytes=7 * 2 ** 30,
                        extra={"storage_bytes_status": "LIKELY_INCOMPLETE"})
    money_out, refusal = price(snapshot, rates)
    assert refusal is None
    assert money_out.usd_per_month == Decimal("0.05") * 7
    assert money_out.to_cell() == "0.35" and money_out.sku == "SYNTH-SNAP-REG-01"


def test_a_snapshot_whose_size_was_never_observed_is_UNSIZED():
    rates = load([SNAPSHOT_REGIONAL], [])
    _money_out, refusal = price(resource("Snapshot", "nightly", location="us-central1"), rates)
    assert refusal.state == model.UNSIZED
    assert refusal.reason == "snapshot storageBytes unobserved"


def test_a_snapshot_is_priced_where_its_storage_locations_says_rather_than_where_it_is():
    """A standard snapshot is global, so its row's location is `global`, which has no rate table.
    `Snapshot.storageLocations` says where the bytes are stored, for example `["us"]`
    (`docs/API-EVIDENCE.md` §Snapshots and images).

    The two records have different prices: `us` must reach the area record at 0.065 and
    `us-central1` the region record at 0.05. The storage location is looked up as given, so `us`
    is not treated as "no region"."""
    rates = load([SNAPSHOT_REGIONAL, SNAPSHOT_AREA], [])

    snapshot = resource("Snapshot", "eren-gpu-20261013", location="global",
                        size_bytes=10 * model.GIB, extra={"storage_locations": "us"})
    money_out, refusal = price(snapshot, rates)
    assert refusal is None
    assert money_out.sku == "34CF-7D88-5D40"
    assert money_out.usd_per_month == Decimal("0.065") * 10
    row = model.Row.of(snapshot, money_out, refusal)
    assert row.location == "global"                    # the resource's own location
    assert row.evidence["storage_locations"] == "us"   # where its bytes are stored

    in_a_region = resource("Snapshot", "asj-debug-20250416", location="global",
                          size_bytes=10 * model.GIB, extra={"storage_locations": "us-central1"})
    money_out, refusal = price(in_a_region, rates)
    assert refusal is None and money_out.sku == "SYNTH-SNAP-REG-01"
    assert money_out.usd_per_month == Decimal("0.05") * 10


def test_a_snapshot_whose_bytes_sit_in_two_places_names_them_both_and_prices_neither():
    """No record covers two storage places, so the row is refused and the reason names both.
    A record is staged for each place, so the refusal is a refusal to choose, not a missing rate.

    The pair is synthetic. Each value is a real Cloud Storage multi-region."""
    in_eu = sku("SYNTH-SNAP-EU-01", "Storage PD Snapshot in EU", group="PDSnapshot",
                tiers=[(0, money("0", 65000000))], regions=("eu",))
    rates = load([SNAPSHOT_AREA, in_eu], [])
    snapshot = resource("Snapshot", "two-place-snap", location="global", size_bytes=10 * model.GIB,
                        extra={"storage_locations": "US,EU"})
    money_out, refusal = price(snapshot, rates)
    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == ("Snapshot.storageLocations named 2 places (US, EU), "
                              "and picking one is picking a bill")
    row = model.Row.of(snapshot, money_out, refusal)
    assert "US" in row.evidence["reason"] and "EU" in row.evidence["reason"]
    assert ";" not in row.evidence["reason"]      # `;` is the evidence separator; it would cut the cell
    assert model.cells(row)[6] == "NA"


def test_a_snapshot_that_named_no_place_says_which_of_the_two_silences_it_is():
    """A missing `storageLocations` and an empty one are different facts. The collector marks the
    empty case with `storage_locations_status`, and the reason says which one happened. Neither
    reason blames the price sheet."""
    rates = load([SNAPSHOT_AREA], [])
    never_asked = resource("Snapshot", "silent", location="global", size_bytes=model.GIB)
    answered_empty = resource("Snapshot", "empty", location="global", size_bytes=model.GIB,
                              extra={"storage_locations_status": "answered-empty"})
    silence = price(never_asked, rates)[1]
    empty = price(answered_empty, rates)[1]

    assert silence.reason == ("snapshot storage place unobserved: 'global' names no rate table "
                              "and the reply carried no Snapshot.storageLocations")
    assert empty.reason == ("snapshot storage place unobserved: 'global' names no rate table "
                            "and Snapshot.storageLocations arrived naming nothing")
    for refusal in (silence, empty):
        assert refusal.state == model.UNPRICED
        assert "no dated Storage PD Snapshot rate" not in refusal.reason


def test_a_place_the_sheet_has_nothing_for_refuses_with_the_sheets_reason_not_our_own():
    """An area with no snapshot record in the sheet is looked up and refused for lack of a rate.
    It must not be refused earlier as "no region", which would blame the collector instead."""
    rates = load([SNAPSHOT_REGIONAL, SNAPSHOT_AREA], [])
    _money_out, refusal = price(
        resource("Snapshot", "asia-snap", location="global", size_bytes=model.GIB,
                 extra={"storage_locations": "asia"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated Storage PD Snapshot rate for asia"


def test_an_area_names_the_snapshot_record_and_a_bucket_names_its_own():
    """Snapshots and buckets can both have an area as their location, and both look up the area's
    rate. Each kind uses its own record: `34CF-7D88-5D40` for a snapshot at 0.065 per GiB and
    `0D5D-6E23-4250` for a Standard bucket at 0.026 (`docs/API-EVIDENCE.md` §Twelve Cloud Storage
    capacity records offered at an area). The rates differ so a mix-up would show."""
    rates = load([SNAPSHOT_AREA], [STANDARD_US_MULTI])
    money_out, refusal = price(
        resource("Snapshot", "eren-gpu-20261013", location="global", size_bytes=model.GIB,
                 extra={"storage_locations": "us"}), rates)
    assert refusal is None and money_out.sku == "34CF-7D88-5D40"

    bucket = resource("Bucket", "lab-multi", location="US", size_bytes=100 * model.GIB,
                      extra={"storage_class": "STANDARD"})
    money_out, refusal = price(bucket, rates)
    assert refusal is None and money_out.sku == "0D5D-6E23-4250"
    assert money_out.usd_per_month == Decimal("2.60")


def test_a_custom_image_looks_for_its_own_record_and_refuses_to_borrow_the_snapshot_rate():
    """With no image record the row is `UNPRICED` and names the lookup. Snapshot storage is a
    different SKU, so its rate is not used."""
    rates = load([SNAPSHOT_REGIONAL], [])
    _money_out, refusal = price(
        resource("Image", "seq-image", location="us-central1", size_bytes=2 ** 30), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated Storage PD Image rate for us-central1"

    image = sku("IMG1-0001-0001", "Storage PD Image", tiers=[(0, money("0", 60000000))])
    money_out, refusal = price(resource("Image", "seq-image", location="us-central1",
                                        size_bytes=2 ** 30),
                               load([SNAPSHOT_REGIONAL, image], []))
    assert refusal is None and money_out.sku == "IMG1-0001-0001"
    assert money_out.usd_per_month == Decimal("0.06")

    # `storageLocations` is a `Snapshot` field. An image row that carries one still looks up an
    # image rate.
    _money_out, by_storage_locations = price(
        resource("Image", "seq-image", location="global", size_bytes=2 ** 30,
                 extra={"storage_locations": "us"}), load([SNAPSHOT_AREA], []))
    assert by_storage_locations.state == model.UNPRICED
    assert "Storage PD Image" in by_storage_locations.reason


def test_a_bucket_price_is_gib_over_2_to_the_30_not_decimal_gb():
    """1500 GiB is 1610612736000 bytes. At 0.02 per GiB-month that is 30.00. Dividing by 10**9
    instead would print 32.21, which is 7.37% too high."""
    rates = load([], [STANDARD_FLAT])
    size = 1500 * model.GIB
    bucket = resource("Bucket", "lab-sequences", location="us-central1", size_bytes=size,
                      extra={"storage_class": "STANDARD"})
    money_out, refusal = price(bucket, rates)
    assert refusal is None
    assert money_out.usd_per_month == Decimal("0.02") * 1500
    assert money_out.to_cell() == "30.00" and money_out.unit == "GiBy.mo"
    assert money_out.sku == "E5F0-6A5D-7BAD" and money_out.as_of == "2026-01-01"
    decimal_gib = Decimal(size) / Decimal(10) ** 9        # the wrong conversion
    assert decimal_gib == Decimal("1610.612736")          # 1500 GiB read as 1610.6 GB
    wrong = Decimal("0.02") * decimal_gib
    assert wrong == Decimal("32.21225472")
    assert wrong - money_out.usd_per_month == Decimal("2.21225472")
    assert wrong / money_out.usd_per_month == Decimal("1.073741824")


def test_a_bucket_with_a_free_first_tier_is_billed_on_the_ladder_not_the_top_rate():
    stepped = sku("STEP-0001-0001", "Standard Storage US Regional", group="RegionalStorage",
                  tiers=[(0, money("0", 0)), (5, money("0", 20000000))],
                  regions=("us-central1",))
    rates = load([], [stepped])
    money_out, _refusal = price(
        resource("Bucket", "lab-sequences", location="us-central1", size_bytes=1500 * model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert money_out.usd_per_month == Decimal("0.02") * 1495      # 5 GiB free, then 1495 billable


def test_a_dual_region_record_in_the_same_group_cannot_answer_a_regional_bucket():
    """The dual-region record has the same region and group at 20x the price. Variant records are
    excluded before the one-candidate check, so the bucket still gets a price."""
    dual = sku("DUAL-REG-0001", "Standard Storage Iowa Dual-region", group="RegionalStorage",
               tiers=[(0, money("0", 400000000))], regions=("us-central1",))
    rates = load([], [STANDARD_FLAT, dual])
    money_out, refusal = price(
        resource("Bucket", "lab-sequences", location="us-central1", size_bytes=1500 * model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal is None and money_out.usd_per_month == Decimal("30.00")


def test_a_legacy_multi_region_bucket_is_priced_on_the_multi_region_table_it_owns():
    multi = sku("0D5D-6E23-4250", "Standard Storage US Multi-region",
                group="MultiRegionalStorage", tiers=[(0, money("0", 20000000))],
                regions=("us-central1", "us-west1"),
                geo={"type": "MULTI_REGIONAL", "regions": ["us-central1", "us-west1"]})
    rates = load([], [multi])
    money_out, refusal = price(
        resource("Bucket", "lab-old-data", location="us-central1", size_bytes=1500 * model.GIB,
                 extra={"storage_class": "MULTI_REGIONAL"}), rates)
    assert refusal is None and money_out.usd_per_month == Decimal("30.00")


def test_a_bucket_the_byte_gauge_never_answered_for_is_UNSIZED_not_zero():
    """An empty bucket is free. A bucket with no size reading is not known to be empty."""
    rates = load([], [STANDARD_FLAT])
    _money_out, refusal = price(
        resource("Bucket", "quiet", location="us-central1",
                 extra={"storage_class": "STANDARD",
                        "not_measured": "byte gauge emitted no series"}), rates)
    assert refusal.state == model.UNSIZED
    assert refusal.reason == "byte gauge emitted no series"


def test_a_bucket_in_a_location_that_is_not_one_region_is_unpriced_rather_than_guessed():
    rates = load([], [STANDARD_FLAT])
    _money_out, refusal = price(
        resource("Bucket", "world", location="global", size_bytes=model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated standard storage rate for global"


# ------------------------------------------------ the region and the description head
#
# Locations, regions and descriptions in this section are real values from 2026-10-20 reads.
# Bucket locations arrive upper case (`US-CENTRAL1`, `US`). Catalog `serviceRegions` are lower case
# (`us-east4`, `asia-east2`). Descriptions include `Standard Storage Hong Kong` beside
# `Autoclass Standard Storage …`, and related snapshot records such as
# `Extreme PD Instant Snapshot data storage in …`. Ids marked SYNTH are made up because the read
# did not record them.

#: `Standard Storage Hong Kong` and `asia-east2` are real. The `skuId` is synthetic.
STANDARD_HONG_KONG = sku("SYNTH-HK-0001", "Standard Storage Hong Kong", group="RegionalStorage",
                         tiers=[(0, money("0", 20000000))], regions=("asia-east2",))


def test_a_bucket_location_matches_the_catalog_record_whatever_case_each_side_used():
    """`buckets.list` returns `US-CENTRAL1` and `serviceRegions` has `us-central1`, so the match
    ignores case. Case is folded only for the comparison. `Rate.regions` and the row's `location`
    cell keep their original spelling."""
    rates = load([], [STANDARD_FLAT])
    bucket = resource("Bucket", "lab-sequences", location="US-CENTRAL1", size_bytes=1500 * model.GIB,
                      extra={"storage_class": "STANDARD"})
    money_out, refusal = price(bucket, rates)
    assert refusal is None
    assert money_out.sku == "E5F0-6A5D-7BAD" and money_out.usd_per_month == Decimal("30.00")
    assert [rate.regions for rate in rates.rates] == [("us-west1", "us-central1", "us-east1")]
    assert model.cells(model.Row.of(bucket, money_out, refusal))[4] == "US-CENTRAL1"


def test_a_lowercase_verbatim_region_matches_the_upper_case_location_the_api_returned():
    """Real values on both sides: `Standard Storage Hong Kong` at `asia-east2`, and an upper-case
    bucket location. This also shows a plain capacity record prices a Standard bucket. The
    Autoclass record with a similar description is refused in a later test."""
    rates = load([], [STANDARD_HONG_KONG])
    money_out, refusal = price(
        resource("Bucket", "lab-images", location="ASIA-EAST2", size_bytes=100 * model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal is None
    assert money_out.sku == "SYNTH-HK-0001" and money_out.usd_per_month == Decimal("2.00")


#: Two synthetic records an area bucket must not price on. `Standard Storage US Regional` offered at
#: `us` is a regional record with an area key, which `location.lower()` would match. The real record
#: (`E5F0-6A5D-7BAD`) names three regions and no area. `Nearline Storage` at `eu` has no place in
#: its description.
AREA_KEYED_REGIONAL = sku("SYNTH-AREA-WRONG-1", "Standard Storage US Regional",
                          group="RegionalStorage", tiers=[(0, money("0", 20000000))],
                          regions=("us",))
PLAIN_NEARLINE_EU = sku("SYNTH-AREA-WRONG-2", "Nearline Storage",
                        group="NearlineStorage", tiers=[(0, money("0", 10000000))],
                        regions=("eu",))


@pytest.mark.parametrize("storage_class, location, wrong, right, sku_id, cell", [
    ("STANDARD", "US", AREA_KEYED_REGIONAL, STANDARD_US_MULTI, "0D5D-6E23-4250", "2.60"),
    ("NEARLINE", "EU", PLAIN_NEARLINE_EU, NEARLINE_EU_MULTI, "4CF0-069A-15D9", "1.50"),
])
def test_a_bucket_in_an_area_is_priced_on_the_areas_own_record_and_never_on_a_regions(
        storage_class, location, wrong, right, sku_id, cell):
    """`US` and `EU` are areas, not regions. The matching record is titled for the area, is in the
    area's group, and is offered at the area's key. A plain `location.lower()` would ask for `eu`,
    where real Autoclass records are offered (`docs/API-EVIDENCE.md` §Twelve Cloud Storage capacity
    records offered at an area). The right records cost 0.026 and 0.015; the wrong ones 0.02 and
    0.01."""
    rates = load([], [wrong, right])
    bucket = resource("Bucket", "lab-world", location=location, size_bytes=100 * model.GIB,
                      extra={"storage_class": storage_class})
    money_out, refusal = price(bucket, rates)
    assert refusal is None and money_out.sku == sku_id
    assert model.Row.of(bucket, money_out, refusal).money.to_cell() == cell


@pytest.mark.parametrize("storage_class, location, only_record", [
    ("STANDARD", "US", AREA_KEYED_REGIONAL),
    ("NEARLINE", "EU", PLAIN_NEARLINE_EU),
])
def test_an_area_with_no_area_record_refuses_and_names_the_lookup_as_an_area(
        storage_class, location, only_record):
    """With only a region's record in the sheet the row is refused, and the reason says it looked
    for an area rate. "no dated standard storage rate for US" would wrongly suggest Google has no
    US price."""
    rates = load([], [only_record])
    bucket = resource("Bucket", "lab-world", location=location, size_bytes=100 * model.GIB,
                      extra={"storage_class": storage_class})
    money_out, refusal = price(bucket, rates)
    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == f"no dated {storage_class.lower()} storage area rate for {location}"
    cells = model.cells(model.Row.of(bucket, money_out, refusal))
    assert cells[6] == "NA" and cells[10] == "UNPRICED"      # no number at all


def test_a_class_that_means_regional_refuses_an_area_instead_of_being_talked_into_one():
    """`REGIONAL` is a legacy class for regional buckets, so a location of `US` contradicts it. The
    area record is staged, so the reason names the contradiction rather than a missing rate."""
    rates = load([], [STANDARD_US_MULTI])
    _money_out, refusal = price(
        resource("Bucket", "lab-legacy-regional", location="US", size_bytes=model.GIB,
                 extra={"storage_class": "REGIONAL"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == ("REGIONAL is a regional placement and US names an area, "
                              "so no single record answers the pair")


def test_an_autoclass_area_record_cannot_answer_a_bucket_just_because_it_names_the_area():
    """Real Autoclass records are offered at `us`, `europe` and `eu`. For example, `C019-C3D4-B615`
    "Autoclass Standard Storage US Multi-region" is in group `Storage` at 0.026. This synthetic copy
    sits in the group the lookup asks for, so only the description match keeps it out."""
    autoclass = sku("SYNTH-AUTO-AREA-1", "Autoclass Standard Storage US Multi-region",
                    group="MultiRegionalStorage", tiers=[(0, money("0", 1000000))], regions=("us",))
    rates = load([], [autoclass, STANDARD_US_MULTI])
    bucket = resource("Bucket", "lab-autoclass", location="US", size_bytes=100 * model.GIB,
                      extra={"storage_class": "STANDARD"})
    money_out, refusal = price(bucket, rates)
    assert refusal is None and money_out.sku == "0D5D-6E23-4250"
    assert money_out.usd_per_month == Decimal("2.60")     # not 0.10, the Autoclass price


def test_two_area_records_refuse_rather_than_choose_the_cheaper():
    """Two records with the same description at the same area key are ambiguous. The row is refused
    and the reason names both ids."""
    second = sku("SYNTH-AREA-DUP-01", "Standard Storage US Multi-region",
                 group="MultiRegionalStorage", tiers=[(0, money("0", 1000000))], regions=("us",))
    rates = load([], [second, STANDARD_US_MULTI])
    _money_out, refusal = price(
        resource("Bucket", "lab-two-area-records", location="US", size_bytes=100 * model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal.state == model.UNPRICED
    assert "SYNTH-AREA-DUP-01" in refusal.reason and "0D5D-6E23-4250" in refusal.reason
    assert "refusing to pick one" in refusal.reason


def test_the_two_US_buckets_of_the_2026_10_21_pass_price_on_the_area_record():
    """Real byte counts from two `US` buckets of the 2026-10-21 run, priced on `0D5D-6E23-4250` at
    0.026. The regional record (0.02) is staged too, so the cell shows which record was used.
    `STANDARD_FLAT` has no free tier; the free tier is tested separately."""
    rates = load([], [STANDARD_US_MULTI, STANDARD_FLAT])
    for item, size_bytes, cell in [("nk-projects", 80955020698, "1.96"),
                                   ("us.artifacts.example-lab-project.appspot.com",
                                    167164117487, "4.05")]:
        bucket = resource("Bucket", item, location="US", size_bytes=size_bytes,
                          extra={"storage_class": "STANDARD"})
        money_out, refusal = price(bucket, rates)
        assert refusal is None and money_out.sku == "0D5D-6E23-4250"
        assert model.Row.of(bucket, money_out, refusal).money.to_cell() == cell


def test_a_refusal_names_the_location_the_cloud_returned_not_the_one_we_folded():
    """The reason prints the location as the API returned it (`US-EAST4`), not the lower-case form
    used for matching."""
    rates = load([], [STANDARD_FLAT])
    _money_out, refusal = price(
        resource("Bucket", "lab-ashburn", location="US-EAST4", size_bytes=model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated standard storage rate for US-EAST4"
    assert "us-east4" not in refusal.reason


@pytest.mark.parametrize("location", ["US", "EU", "ASIA", "global"])
def test_an_area_code_location_is_refused_without_a_region_key_being_invented(location):
    """Each location that is not a region is refused, with the location as given."""
    rates = load([], [STANDARD_FLAT])
    _money_out, refusal = price(
        resource("Bucket", "lab-area", location=location, size_bytes=model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason.endswith(f" for {location}")


def test_a_global_snapshot_with_no_storage_place_refuses_and_names_the_field():
    """A standard snapshot's location is `global`, which has no rate table. With no
    `storageLocations` in the reply, the reason names that field rather than blaming the sheet.
    The area record and a `global`-offered record are both staged, and neither may be used."""
    global_shaped = sku("SYNTH-GLOB-001", "Storage PD Snapshot", group="PDSnapshot",
                        tiers=[(0, money("0", 65000000))], regions=("global",))
    rates = load([SNAPSHOT_AREA, global_shaped], [])
    snapshot = resource("Snapshot", "eren-gpu-20261001", location="global", size_bytes=2 ** 30)
    money_out, refusal = price(snapshot, rates)
    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == ("snapshot storage place unobserved: 'global' names no rate table "
                              "and the reply carried no Snapshot.storageLocations")
    assert "no dated Storage PD Snapshot rate" not in refusal.reason
    row = model.Row.of(snapshot, money_out, refusal)
    assert row.location == "global" and row.evidence["reason"] == refusal.reason
    assert "storage_locations" not in row.evidence      # unobserved is absent, not a dash
    assert model.cells(row)[6] == "NA"


@pytest.mark.parametrize("description", [
    # Real description shapes from the Compute Engine sheet. The place at the end is filled in;
    # only the start of the description matters.
    "Extreme PD Instant Snapshot data storage in US",
    "Regional Archive Snapshot Early Deletion in US",
    "PD Snapshot Data Transfer between US and Europe",
])
def test_a_neighbouring_snapshot_record_is_never_the_pd_snapshot_rate(description):
    """These are different products whose descriptions contain "Snapshot" and "Storage". Each is
    staged in `PDSnapshot` at `us-central1` at 5.00, against the real record's 0.05, so only the
    description match keeps it out.

    Each is checked twice: with the place from the row's location, and from
    `Snapshot.storageLocations` on a `global` row."""
    impostor = sku("SYNTH-NOTSNAP-01", description, group="PDSnapshot",
                   tiers=[(0, money("0", 5000000000))], regions=("us-central1",))
    rates = load([SNAPSHOT_REGIONAL, impostor], [])
    named_by_location = resource("Snapshot", "eren-gpu-20261001", location="us-central1",
                                 size_bytes=10 * model.GIB)
    named_by_storage_locations = resource("Snapshot", "eren-gpu-20261001", location="global",
                                          size_bytes=10 * model.GIB,
                                          extra={"storage_locations": "us-central1"})
    for snapshot in (named_by_location, named_by_storage_locations):
        money_out, refusal = price(snapshot, rates)
        assert refusal is None, f"{snapshot.location}: {refusal.reason}"
        assert money_out.sku == "SYNTH-SNAP-REG-01"
        assert money_out.usd_per_month == Decimal("0.05") * 10
    only_impostor = load([impostor], [])
    _money_out, refused = price(
        resource("Snapshot", "eren-gpu-20261001", location="us-central1",
                 size_bytes=10 * model.GIB), only_impostor)
    assert refused.state == model.UNPRICED
    assert refused.reason == "no dated Storage PD Snapshot rate for us-central1"


def test_an_autoclass_record_is_never_a_standard_buckets_rate():
    """Autoclass has its own rates. Real Autoclass records are in group `Storage`; this one is
    staged in `RegionalStorage` so only the description match keeps it out. On its own it must
    price nothing."""
    autoclass = sku("SYNTH-AUTO-0001", "Autoclass Standard Storage Hong Kong",
                    group="RegionalStorage", tiers=[(0, money("0", 26000000))],
                    regions=("asia-east2", "us-central1"))
    bucket = resource("Bucket", "lab-images", location="ASIA-EAST2", size_bytes=100 * model.GIB,
                      extra={"storage_class": "STANDARD"})
    money_out, refusal = price(bucket, load([], [STANDARD_HONG_KONG, autoclass]))
    assert refusal is None
    assert money_out.sku == "SYNTH-HK-0001" and money_out.usd_per_month == Decimal("2.00")
    _money_out, refused = price(bucket, load([], [autoclass]))
    assert refused.state == model.UNPRICED
    assert refused.reason == "no dated standard storage rate for ASIA-EAST2"


def test_every_bucket_class_looks_for_its_own_description_head():
    """One record per class, each at a different price, so a wrong mapping in `GCS_CLASSES` shows.
    The `<Class> Storage <Place>` shape is real; the Nearline, Coldline and Archive records for
    Hong Kong are made up."""
    staged = [sku("SYNTH-STD-0001", "Standard Storage Hong Kong", group="RegionalStorage",
                  tiers=[(0, money("0", 20000000))], regions=("asia-east2",)),
              sku("SYNTH-NRN-0001", "Nearline Storage Hong Kong", group="NearlineStorage",
                  tiers=[(0, money("0", 10000000))], regions=("asia-east2",)),
              sku("SYNTH-CLD-0001", "Coldline Storage Hong Kong", group="ColdlineStorage",
                  tiers=[(0, money("0", 4000000))], regions=("asia-east2",)),
              sku("SYNTH-ARC-0001", "Archive Storage Hong Kong", group="ArchiveStorage",
                  tiers=[(0, money("0", 1000000))], regions=("asia-east2",))]
    want = {"STANDARD": ("SYNTH-STD-0001", "2.00"), "NEARLINE": ("SYNTH-NRN-0001", "1.00"),
            "COLDLINE": ("SYNTH-CLD-0001", "0.40"), "ARCHIVE": ("SYNTH-ARC-0001", "0.10")}
    rates = load([], staged)
    for storage_class, (sku_id, amount) in want.items():
        money_out, refusal = price(
            resource("Bucket", "lab-images", location="ASIA-EAST2", size_bytes=100 * model.GIB,
                     extra={"storage_class": storage_class}), rates)
        assert refusal is None, f"{storage_class}: {refusal.reason}"
        assert money_out.sku == sku_id and money_out.to_cell() == amount


def test_a_record_that_names_no_region_and_does_not_claim_to_be_global_matches_nothing():
    rates = load([], [dict(STANDARD_FLAT, serviceRegions=[])])
    _money_out, refusal = price(
        resource("Bucket", "lab-sequences", location="us-central1", size_bytes=model.GIB,
                 extra={"storage_class": "STANDARD"}), rates)
    assert refusal.state == model.UNPRICED


# ------------------------------------------------------------- currency and cells

def test_a_non_usd_rate_prices_and_says_so_in_the_currency_cell():
    """A non-USD row shows its currency in the `currency` column. Nothing converts it."""
    euro = sku("EURO-0001-0001", "Storage PD Snapshot in Europe", group="PDSnapshot",
               tiers=[(0, money("0", 65000000, "EUR"))], regions=("europe-west1",))
    rates = load([euro], [])
    snapshot = resource("Snapshot", "nightly", location="europe-west1", size_bytes=2 ** 30)
    money_out, refusal = price(snapshot, rates)
    assert refusal is None
    assert money_out.currency == "EUR" and money_out.usd_per_month == Decimal("0.065")
    row = model.Row.of(snapshot, money_out, refusal)
    assert row.amount_state == model.MEASURED
    assert model.cells(row)[9] == "EUR"


def test_an_empty_currency_still_prints_a_currency_rather_than_a_blank():
    untagged = sku("NOCURR-0001-01", "Storage PD Snapshot", group="PDSnapshot",
                   tiers=[(0, {"units": "0", "nanos": 65000000})])
    rates = load([untagged], [])
    money_out, refusal = price(
        resource("Snapshot", "nightly", location="us-central1", size_bytes=2 ** 30), rates)
    assert refusal is None and money_out.currency == "USD"


def test_a_kind_with_no_rate_table_is_unpriced_and_not_zero():
    rates = load([PD_SSD], [STANDARD_FLAT])
    # A dataset holds tables that are already priced, so pricing it would count the bytes twice.
    _money_out, refusal = price(
        resource("Dataset", "variants", location="US"), rates)
    assert refusal.state == model.UNPRICED
    assert refusal.reason == "kind Dataset has no price table in v1"


def test_a_priced_row_and_a_refused_row_are_not_confusable_by_the_writer():
    rates = load([PD_SSD], [])
    disk = resource("Disk", "seq-data", location="us-central1-c", size_gb_api=100,
                    extra={"storage_class": "pd-ssd"})
    money_out, refusal = price(disk, rates)
    assert money_out.usd_per_month == Decimal("17")               # 0.17 x 100, one tier
    priced = model.cells(model.Row.of(disk, money_out, refusal))
    assert priced[6:11] == ["17.00", "CATALOG", "2026-01-01", "USD", "MEASURED"]

    unpriced = resource("Disk", "bare", location="us-central1-c", size_gb_api=100)
    money_refused, refusal_out = price(unpriced, rates)
    assert money_refused is None
    cells = model.cells(model.Row.of(unpriced, money_refused, refusal_out))
    assert cells[6] == "NA" and cells[7] == "NA" and cells[9] == "NA"
    assert cells[10] == "UNPRICED" and "disk class not observed" in cells[13]


def test_no_decimal_gb_helper_exists_anywhere_in_the_package():
    """2**30 is the only bytes-to-GiB divisor in the package. A GiB rate applied to `bytes / 1e9`
    overstates the cost by 7.37%.

    The check walks the syntax tree rather than grepping, because `model.py` mentions the divisor
    in a docstring. No numeric literal may equal 10**9, in any spelling, and no helper may use one
    of the banned names.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "gcp_audit"
    banned_names = ("GB_DECIMAL", "DECIMAL_GB", "bytes_to_gb", "gb_from_bytes", "decimal_gigabyte")
    offenders: list[str] = []
    for path in sorted(source.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', '?')}"
            if (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                    and not isinstance(node.value, bool) and node.value == 1_000_000_000):
                offenders.append(f"{where}: the literal {node.value!r} is a decimal gigabyte")
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow)
                    and isinstance(node.left, ast.Constant)
                    and node.left.value in (10, 10.0) and isinstance(node.right, ast.Constant)
                    and node.right.value == 9):
                offenders.append(f"{where}: 10 ** 9 is a decimal gigabyte")
            for name in _defined_names(node):
                if any(banned in name for banned in banned_names):
                    offenders.append(f"{where}: {name} is a decimal-GB helper's name")
    assert offenders == []
    assert model.GIB == 2 ** 30


def _defined_names(node: ast.AST) -> list[str]:
    """Names a statement binds: a def/class name, or the targets of an assignment."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    return []


# --------------------------------------------- custom shapes and extended memory
#
# The records below are real Compute Engine records (`services/6F81-5844-456A`, fetched
# 2026-09-29, offered at `us-central1`, `us-east1` and `us-west1`): skuId, description, unit and
# rate. `n2d-custom-2-24832-ext` is a real machine type; the other shapes are made up.
# `docs/DESIGN.md` §Sizing and money gives the source of the memory ceilings.

CUSTOM_CORE_N2D = sku("55FE-5117-DF8B", "N2D AMD Custom Instance Core running in Americas",
                      family="Compute", group="CPU", unit="h",
                      tiers=[(0, money("0", 28877100))], effective="2026-09-29T07:00:00Z")
CUSTOM_RAM_N2D = sku("CED4-7119-A546", "N2D AMD Custom Instance Ram running in Americas",
                     family="Compute", group="RAM", unit="GiBy.h",
                     tiers=[(0, money("0", 3870300))], effective="2026-09-29T07:00:00Z")
CUSTOM_EXT_N2D = sku("CC77-3AED-816F", "N2D AMD Custom Extended Ram running in Americas",
                     family="Compute", group="RAM", unit="GiBy.h",
                     tiers=[(0, money("0", 8309000))], effective="2026-09-29T07:00:00Z")
CUSTOM_CORE_N1 = sku("ACBC-6999-A1C4", "Custom Instance Core running in Americas",
                     family="Compute", group="CPU", unit="h",
                     tiers=[(0, money("0", 33191550))], effective="2026-09-29T07:00:00Z")
CUSTOM_RAM_N1 = sku("51E2-59BD-7A6E", "Custom Instance Ram running in Americas",
                    family="Compute", group="RAM", unit="GiBy.h",
                    tiers=[(0, money("0", 4446000))], effective="2026-09-29T07:00:00Z")
CUSTOM_EXT_N1 = sku("972B-1B48-9D16", "Custom Extended Instance Ram running in Americas",
                    family="Compute", group="RAM", unit="GiBy.h",
                    tiers=[(0, money("0", 9550000))], effective="2026-09-29T07:00:00Z")
CUSTOM_CORE_N2 = sku("3E94-1AB5-33F5", "N2 Custom Instance Core running in Americas",
                     family="Compute", group="CPU", unit="h",
                     tiers=[(0, money("0", 33191550))], effective="2026-09-29T07:00:00Z")
CUSTOM_RAM_N2 = sku("5304-5146-9088", "N2 Custom Instance Ram running in Americas",
                    family="Compute", group="RAM", unit="GiBy.h",
                    tiers=[(0, money("0", 4448850))], effective="2026-09-29T07:00:00Z")


def custom(machine_type: str, cpus: int, memory_mb: int,
           location: str = "us-central1-a") -> model.Resource:
    """A row `collect_compute` stamped from a custom machine-type name.

    `location` is a zone; `_where` turns it into the region the records above are offered at."""
    return resource("Instance", "custom-vm", location=location, cpus=cpus, memory_mb=memory_mb,
                    extra={"machine_type": machine_type})


def test_a_custom_shape_prices_on_its_family_s_custom_records_not_its_predefined_ones():
    """A custom N1 shape bills on `Custom Instance Core` and `Custom Instance Ram`, not on the
    `N1 Predefined Instance` records, which have different rates. Both pairs are staged, and only
    `-custom-` in the machine type tells them apart."""
    rates = load([CORE_N1, RAM_N1, CUSTOM_CORE_N1, CUSTOM_RAM_N1], [])
    money_out, refusal = price(custom("n1-custom-4-8192", 4, 8192), rates)

    assert refusal is None
    assert money_out.sku == "51E2-59BD-7A6E+ACBC-6999-A1C4"
    assert money_out.usd_per_month == (Decimal("0.03319155") * 4 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.004446") * 8 * pricing.HOURS_PER_MONTH)
    assert money_out.as_of == "2026-09-29"
    assert money_out.unit == "h+GiBy.h@730h/mo"

    predefined = price(resource("Instance", "seq-worker", location="us-central1-c", cpus=4,
                                memory_mb=15360, extra={"machine_type": "n1-standard-4"}), rates)
    assert predefined[1] is None and predefined[0].sku == "2E27-4F75-95CD+6C71-E844-38BC"


def test_an_extended_shape_splits_its_ram_at_the_family_ceiling_and_prices_three_records():
    """`n2d-custom-2-24832-ext` has 2 vCPUs and 24,832 MB with extended memory. The N2D ceiling is
    8.0 GB per vCPU, so 16,384 MB is ordinary custom RAM and the other 8,448 MB (8.25 GiB) is
    extended RAM at 0.008309 rather than 0.0038703 per `GiBy.h`.

    The three records have different rates, so the total is reached only by using each once.
    `sku` lists all three ids.
    """
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D], [])
    money_out, refusal = price(custom("n2d-custom-2-24832-ext", 2, 24832), rates)

    assert refusal is None
    assert money_out.usd_per_month == (Decimal("0.0288771") * 2 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.0038703") * 16 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.008309") * Decimal("8.25")
                                       * pricing.HOURS_PER_MONTH)
    assert money_out.to_cell() == "137.41"
    assert sorted(money_out.sku.split("+")) == sorted(
        ["55FE-5117-DF8B", "CED4-7119-A546", "CC77-3AED-816F"])


def test_the_memory_split_is_the_ceiling_times_the_cores_and_nothing_else():
    """`base = min(total, vcpus × ceiling)`. On N2D, 8 vCPUs allow 64 GiB of ordinary RAM, so a
    96 GiB shape is 64 GiB base and 32 GiB extended. The N1 ceiling is 6.5 GB per vCPU, so the same
    shape on N1 splits at 52 GiB."""
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D], [])
    money_out, refusal = price(custom("n2d-custom-8-98304-ext", 8, 98304), rates)

    assert refusal is None
    assert money_out.usd_per_month == (Decimal("0.0288771") * 8 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.0038703") * 64 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.008309") * 32 * pricing.HOURS_PER_MONTH)

    n1 = load([CUSTOM_CORE_N1, CUSTOM_RAM_N1, CUSTOM_EXT_N1], [])
    n1_out, n1_refusal = price(custom("n1-custom-8-98304-ext", 8, 98304), n1)
    assert n1_refusal is None
    assert n1_out.usd_per_month == (Decimal("0.03319155") * 8 * pricing.HOURS_PER_MONTH
                                    + Decimal("0.004446") * Decimal("52")
                                    * pricing.HOURS_PER_MONTH          # 8 × 6.5 GiB
                                    + Decimal("0.00955") * Decimal("44")
                                    * pricing.HOURS_PER_MONTH)         # the rest


def test_a_shape_under_its_ceiling_needs_no_extended_record():
    """A `-ext` shape whose RAM is under the ceiling has no extended part, so the extended record
    is not used. It is staged at $500 per GiB-hour so any use would show. The shape is made up."""
    dear = sku("SYNTH-EXT-DEAR-01", "N2D AMD Custom Extended Ram running in Americas",
               family="Compute", group="RAM", unit="GiBy.h",
               tiers=[(0, money("500", 0))], effective="2026-09-29T07:00:00Z")
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, dear], [])
    money_out, refusal = price(custom("n2d-custom-2-8192-ext", 2, 8192), rates)

    assert refusal is None
    assert money_out.sku == "55FE-5117-DF8B+CED4-7119-A546"
    assert money_out.usd_per_month == (Decimal("0.0288771") * 2 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.0038703") * 8 * pricing.HOURS_PER_MONTH)


def test_a_missing_extended_record_refuses_the_whole_row_and_names_it():
    """Pricing the row without its 8,448 MB of extended RAM would understate it. The whole row is
    refused, and the reason names the missing record."""
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D], [])
    money_out, refusal = price(custom("n2d-custom-2-24832-ext", 2, 24832), rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == "no dated N2D AMD Custom Extended Ram rate for us-central1"


def test_a_missing_custom_core_refuses_naming_the_core_record():
    rates = load([CUSTOM_RAM_N2D, CUSTOM_EXT_N2D], [])
    money_out, refusal = price(custom("n2d-custom-2-24832-ext", 2, 24832), rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == "no dated N2D AMD Custom Instance Core rate for us-central1"


def test_a_family_with_no_ceiling_on_file_gets_no_split_by_default():
    """A family's ceiling must be quoted from Google's documentation. E2 has no quoted ceiling and
    no extended record, so an E2 shape with extended memory is refused."""
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D,
                  sku("61C6-6F89-DCF4", "E2 Custom Instance Core running in Americas",
                      family="Compute", group="CPU", unit="h",
                      tiers=[(0, money("0", 22902170))], effective="2026-09-29T07:00:00Z"),
                  sku("8D24-CB74-5E70", "E2 Custom Instance Ram running in Americas",
                      family="Compute", group="RAM", unit="GiBy.h",
                      tiers=[(0, money("0", 3069707))], effective="2026-09-29T07:00:00Z")], [])
    money_out, refusal = price(custom("e2-custom-2-8192-ext", 2, 8192), rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert "no per-vCPU memory ceiling on file for the e2 family" in refusal.reason
    assert "n1, n2, n2d" in refusal.reason


def test_extended_memory_on_a_name_that_is_not_custom_refuses():
    """`-ext` is modelled only for custom shapes, because the ceilings apply to custom memory. A
    predefined name ending in `-ext` is refused rather than priced as ordinary RAM."""
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D], [])
    money_out, refusal = price(custom("n2d-standard-2-ext", 2, 24832), rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert "only on a custom machine type" in refusal.reason


def test_the_custom_head_is_anchored_so_one_family_s_record_cannot_answer_another():
    """N1's custom records have no family prefix (`Custom Instance Core`). An N1 lookup must not
    match `N2 Custom Instance Core`, and an N2 lookup must not match N1's. Each family is priced
    against a sheet that holds only the other family's records."""
    n1_row, n2_sheet = price(custom("n1-custom-2-8192", 2, 8192),
                             load([CUSTOM_CORE_N2, CUSTOM_RAM_N2], []))
    assert n1_row is None and n2_sheet.reason == "no dated Custom Instance Core rate for us-central1"

    n2_row, n1_sheet = price(custom("n2-custom-2-8192", 2, 8192),
                             load([CUSTOM_CORE_N1, CUSTOM_RAM_N1], []))
    assert n2_row is None and n1_sheet.reason == "no dated N2 Custom Instance Core rate for us-central1"


def test_a_spot_preemptible_extended_record_is_not_an_ordinary_vm_s_rate():
    """The sheet has `Spot Preemptible N2D AMD Custom Extended Instance RAM…`. Spot has a different
    price, and its description uses upper-case `RAM`. Matching from the start of the description
    keeps it out."""
    impostor = sku("SYNTH-SPOT-EXT-01",
                   "Spot Preemptible N2D AMD Custom Extended Instance RAM running in Americas",
                   family="Compute", group="RAM", unit="GiBy.h",
                   tiers=[(0, money("0", 99000000))], effective="2026-09-29T07:00:00Z")
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D, impostor], [])
    money_out, refusal = price(custom("n2d-custom-2-24832-ext", 2, 24832), rates)

    assert refusal is None and "CC77-3AED-816F" in money_out.sku
    assert "SYNTH-SPOT-EXT-01" not in money_out.sku


def test_the_ceiling_constants_carry_their_sentences_and_their_fetch_date():
    """The ceiling decides which rate RAM bills at, so every entry must open with a quotation from
    Google's page and name the date it was fetched.
    """
    assert set(pricing.RAM_CEILINGS) <= set(pricing.MACHINE_FAMILIES)
    for ceiling in pricing.RAM_CEILINGS.values():
        assert ceiling.gib_per_vcpu > 0
        assert "general-purpose-machines" in ceiling.because
        assert "2026-10-20" in ceiling.because
        assert ceiling.because.lstrip().startswith('"'), "a ceiling with no quotation is a guess"
    assert ("For N2D machine types, select between 0.5 GB and 8.0 GB per vCPU in 256 MB increments."
            in pricing.RAM_CEILINGS["n2d"].because)
    assert ("For N1 machine types, select between 0.9 GB and 6.5 GB per vCPU, inclusive."
            in pricing.RAM_CEILINGS["n1"].because)
    assert ("For the N2 machine series, select between 0.5 GB and 8.0 GB per vCPU in 256 MB increments."
            in pricing.RAM_CEILINGS["n2"].because)


def test_a_family_s_records_are_named_even_where_no_record_has_been_read():
    """`custom_extended` is empty for E2 because no such record is on file. Borrowing another
    family's description would invent a rate."""
    assert pricing.MACHINE_FAMILIES["e2"].custom_extended == ""
    assert pricing.MACHINE_FAMILIES["n2d"].custom_extended == "N2D AMD Custom Extended Ram"


# ------------------------------------------------------ a zero that a listing settled

def test_a_bucket_whose_one_object_page_came_back_empty_prices_at_zero_and_says_which_read():
    """The gauge was silent, the bucket is not versioned, and a one-object listing came back empty
    and complete, so there is nothing to bill. `size_source` records which read gave the zero, and
    the row is `MEASURED`."""
    rates = load([], [STANDARD_FLAT])
    bucket = resource("Bucket", "dataproc-temp-us-central1-000000000000-abcde",
                      location="us-central1", size_bytes=0,
                      extra={"storage_class": "STANDARD",
                             "size_source_note": ""})
    bucket.size_source = "storage.objects.list"
    money_out, refusal = price(bucket, rates)

    assert refusal is None
    assert money_out.usd_per_month == 0 and money_out.to_cell() == "0.00"
    assert money_out.sku == "E5F0-6A5D-7BAD" and money_out.as_of == "2026-01-01"
    row = model.Row.of(bucket, money_out, refusal)
    assert row.amount_state == model.MEASURED
    cells = model.cells(row)
    assert cells[6] == "0.00" and cells[7] == "CATALOG"
    assert "size_source=storage.objects.list" in cells[-1]


@pytest.mark.parametrize("probe", sorted(pricing.OBJECT_PROBE_REASONS))
def test_each_thing_the_listing_could_not_settle_refuses_with_its_own_sentence(probe):
    """Each probe outcome has its own reason, so the reader checks the right thing."""
    rates = load([], [STANDARD_FLAT])
    row = resource("Bucket", "lab-quiet", location="us-central1",
                   extra={"storage_class": "STANDARD", "objects_probe": probe,
                          "not_measured": "byte gauge asked about 7 days emitted no series"})
    money_out, refusal = price(row, rates)

    assert money_out is None and refusal.state == model.UNSIZED
    assert refusal.reason == pricing.OBJECT_PROBE_REASONS[probe]
    assert "7 days" not in refusal.reason      # the probe's sentence, not the gauge's


def test_a_silent_bucket_with_no_probe_recorded_prints_the_gauge_s_own_sentence():
    """`collect_storage` writes the gauge's reason into `not_measured`. A row with no probe prints
    that reason unchanged."""
    rates = load([], [STANDARD_FLAT])
    row = resource("Bucket", "quiet", location="us-central1",
                   extra={"storage_class": "STANDARD",
                          "not_measured": "byte gauge asked about 7 days emitted no series"})
    _money_out, refusal = price(row, rates)

    assert refusal.state == model.UNSIZED
    assert refusal.reason == "byte gauge asked about 7 days emitted no series"


def test_ram_above_the_ceiling_without_the_ext_marker_refuses_rather_than_underpricing():
    """Memory above the family's ceiling without `-ext` in the name contradicts the documented
    ceiling. Pricing it all as ordinary RAM would understate the bill, so the row is refused and the
    reason gives the numbers."""
    rates = load([CUSTOM_CORE_N2D, CUSTOM_RAM_N2D, CUSTOM_EXT_N2D], [])
    money_out, refusal = price(custom("n2d-custom-8-98304", 8, 98304), rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert refusal.reason == (
        "98304 MB on 8 vCPUs is above the n2d family's 8.0 GB-per-vCPU ceiling and the name does "
        "not carry -ext, so which rate the excess bills at is unobserved")


def test_a_predefined_shape_is_never_split_at_the_ceiling():
    """The ceiling applies to custom shapes only. Predefined types such as `n1-megamem-96` have more
    than 6.5 GB per vCPU and are not billed at extended rates. The 96 vCPU, 864 GiB shape is made
    up; the test checks that no extended record is used."""
    dear_ext = sku("SYNTH-EXT-DEAR-02", "Custom Extended Instance Ram running in Americas",
                   family="Compute", group="RAM", unit="GiBy.h", tiers=[(0, money("9", 0))],
                   effective="2026-01-01T00:00:00Z")
    rates = load([CORE_N1, RAM_N1, CUSTOM_EXT_N1, dear_ext], [])
    money_out, refusal = price(
        resource("Instance", "big-memory", location="us-central1-a", cpus=96,
                 memory_mb=864 * 1024, extra={"machine_type": "n1-megamem-96"}), rates)

    assert refusal is None
    assert money_out.sku == "2E27-4F75-95CD+6C71-E844-38BC"
    assert money_out.usd_per_month == (Decimal("0.031611") * 96 * pricing.HOURS_PER_MONTH
                                       + Decimal("0.004237") * 864 * pricing.HOURS_PER_MONTH)


# ----------------------------------------------------- BigQuery: two records, one table

# Four real records from the 2026-10-22 capture of `services/24E6-581D-38E5`
# (`docs/API-EVIDENCE.md` §The BigQuery sheet). Everything but `effectiveTime` is real; the real
# date is after `AS_OF`, so the fixture default is used.
#
# The area pair, used for multi-region datasets, is 0.02 active and 0.01 long-term, offered at `us`
# and `europe`. The `us-central1` pair is 0.023 and 0.016. A `US` row priced at 0.023 used the
# region record and is 15% high.
BQ_ACTIVE_AREA = sku("947D-3B46-7781", "Active Logical Storage", family="ApplicationServices",
                     group="ActiveStorage", regions=("us", "europe"),
                     tiers=[(0, money("0", 0)), (10, money("0", 20000000))])
BQ_LONG_TERM_AREA = sku("993F-6B6B-DCC4", "Long Term Logical Storage",
                        family="ApplicationServices", group="LongTermStorage",
                        regions=("us", "europe"),
                        tiers=[(0, money("0", 0)), (10, money("0", 10000000))])
BQ_ACTIVE_USC1 = sku("0018-A5A0-9D6D", "Active Logical Storage (us-central1)",
                     family="ApplicationServices", group="ActiveStorage",
                     tiers=[(0, money("0", 0)), (10, money("0", 23000000))])
BQ_LONG_TERM_USC1 = sku("55B3-2F73-B6A3", "Long Term Logical Storage (us-central1)",
                        family="ApplicationServices", group="LongTermStorage",
                        tiers=[(0, money("0", 0)), (10, money("0", 16000000))])
BQ_ALL = [BQ_ACTIVE_AREA, BQ_LONG_TERM_AREA, BQ_ACTIVE_USC1, BQ_LONG_TERM_USC1]

#: The split, in bytes, that `collect_bigquery` accepts: halves that add to the `numBytes` on the row.
SPLIT_20_20 = {"active_logical_bytes": 20 * model.GIB, "long_term_logical_bytes": 20 * model.GIB}


def table(active: int, long_term: int, location: str = "US") -> model.Resource:
    """A Table row with the age split settled, sized at the sum of its halves."""
    return resource("Table", "gcp_billing_export", location=location,
                    size_bytes=(active + long_term if active is not None else None),
                    extra=dict(SPLIT_20_20 if (active == 20 * model.GIB and long_term == 20 * model.GIB)
                               else {"active_logical_bytes": active,
                                     "long_term_logical_bytes": long_term}))


def test_a_table_prices_its_two_halves_on_two_records_and_names_both():
    """One row, two records, one number, as an `Instance` row does for core and RAM. `sku` lists
    both ids."""
    rates = load([], [], BQ_ALL)
    money_out, refusal = price(table(20 * model.GIB, 20 * model.GIB), rates)

    assert refusal is None
    # First 10 GiB-month of each tier is free, so (20 - 10) x 0.02 + (20 - 10) x 0.01.
    assert money_out.to_cell() == "0.30"
    assert money_out.sku == "947D-3B46-7781+993F-6B6B-DCC4"
    assert money_out.unit == "GiBy.mo"


def test_a_us_table_prices_on_the_area_pair_and_never_on_the_region_reached_by_folding():
    """30.58 GiB active is $0.41 on the `us` record and would be $0.47 on the `us-central1` one.
    The row's location is `US`, an area, so only the area record may price it."""
    size = 32833791428                        # a real billing-export table, 2026-10-22
    res = resource("Table", "gcp_billing_export", location="US", size_bytes=size,
                   extra={"active_logical_bytes": size, "long_term_logical_bytes": 0})

    area = load([], [], [BQ_ACTIVE_AREA, BQ_LONG_TERM_AREA, BQ_ACTIVE_USC1, BQ_LONG_TERM_USC1])
    money_out, refusal = price(res, area)
    assert refusal is None
    assert money_out.to_cell() == "0.41"
    assert money_out.sku == "947D-3B46-7781"   # long-term is zero, so its record is not used

    folded = load([], [], [BQ_ACTIVE_USC1, BQ_LONG_TERM_USC1])
    _money_out, refused = price(res, folded)
    assert refused.state == model.UNPRICED
    assert refused.reason == "no dated active logical storage rate for US"


def test_a_regional_table_takes_its_own_regions_record():
    rates = load([], [], BQ_ALL)
    res = resource("Table", "t", location="us-central1", size_bytes=20 * model.GIB,
                   extra={"active_logical_bytes": 20 * model.GIB, "long_term_logical_bytes": 0})
    money_out, refusal = price(res, rates)

    assert refusal is None
    assert money_out.to_cell() == "0.23"      # (20 - 10) x 0.023, the us-central1 record
    assert money_out.sku == "0018-A5A0-9D6D"


def test_an_asia_table_names_the_pair_that_is_not_there_instead_of_borrowing_one():
    """The sheet has no `asia` logical-storage record, only the `us`/`europe` pair and single
    regions. An `ASIA` table is refused, and the reason names the area."""
    rates = load([], [], BQ_ALL)
    _money_out, refusal = price(table(20 * model.GIB, 20 * model.GIB, location="ASIA"), rates)

    assert refusal.state == model.UNPRICED
    assert refusal.reason == "no dated active logical storage rate for ASIA"


def test_a_split_that_was_never_answered_is_unpriced_and_says_which_read_would_settle_it():
    """A table with no active/long-term split is refused rather than priced at a guessed tier."""
    rates = load([], [], BQ_ALL)
    res = resource("Table", "variants", location="us-central1", size_bytes=40 * model.GIB)
    _money_out, refusal = price(res, rates)

    assert refusal.state == model.UNPRICED
    assert "active/long-term byte split was not answered" in refusal.reason


def test_a_split_that_does_not_add_up_refuses_rather_than_choosing_a_tier():
    """`numBytes` excludes the streaming buffer and `numTotalLogicalBytes` does not, so a table
    being streamed into can report sizes that disagree. The row is refused and quotes the
    collector's reason."""
    rates = load([], [], BQ_ALL)
    res = resource("Table", "streamed", location="US", size_bytes=30 * model.GIB,
                   extra={"split_disagrees": "active 21474836480 + long-term 10737418240 "
                                             "!= numBytes 32212254720"})
    _money_out, refusal = price(res, rates)

    assert refusal.state == model.REFUSED
    assert refusal.reason.startswith("the byte counts disagree, so no tier was taken: ")
    assert "numBytes 32212254720" in refusal.reason


def test_a_zero_half_is_not_asked_of_a_record_it_cannot_bill():
    """A half with 0 bytes bills nothing, so its record is not looked up. Otherwise a missing
    record for that half would refuse a row that can be priced."""
    rates = load([], [], [BQ_LONG_TERM_AREA])         # no active record staged at all
    res = resource("Table", "cold", location="US", size_bytes=10 * model.GIB,
                   extra={"active_logical_bytes": 0, "long_term_logical_bytes": 10 * model.GIB})
    money_out, refusal = price(res, rates)

    assert refusal is None
    assert money_out.to_cell() == "0.00"      # 10 GiB is inside the ladder's free step
    assert money_out.sku == "993F-6B6B-DCC4"


def test_the_prose_that_counts_the_priced_kinds_says_the_number_of_priced_kinds():
    """The comment above `KIND_SHEET` says "these N kinds". N must match the table's size, so
    adding a kind without updating the comment fails this test."""
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
    source = Path(pricing.__file__).read_text(encoding="utf-8")
    counted = [word for word in words if f"these {word} kinds" in source]

    assert counted == [next(w for w, n in words.items() if n == len(pricing.KIND_SHEET))], (
        f"`KIND_SHEET` has {len(pricing.KIND_SHEET)} kinds and the comment above it says {counted}")
    assert "Dataset" not in pricing.KIND_SHEET       # its tables are already priced


@pytest.mark.parametrize("extra,phrase", [
    ({"local_ssd": "1"}, "1 local SSD(s) not priced here"),
    ({"shared_cpu": "true"}, "shared-core machine type bills a fractional vCPU"),
    ({"provisioning_model": "FLEX_START"}, "FLEX_START provisioning bills at rates not priced here"),
])
def test_a_vm_with_charges_the_price_omits_is_unpriced_not_mispriced(extra, phrase):
    rates = load([CORE_N1, RAM_N1], [])
    vm = resource("Instance", "worker", location="us-central1-c", cpus=4, memory_mb=15360,
                  extra={"machine_type": "n1-standard-4", **extra})
    money_out, refusal = price(vm, rates)

    assert money_out is None and refusal.state == model.UNPRICED
    assert phrase in refusal.reason


# Real records (`docs/API-EVIDENCE.md` §Compute Engine records for Spot VMs, GPUs and static IPs).
# The Spot and static IP records are from the 2026-09-29 capture; the GPU records are from Google's
# public SKU groups.
SPOT_CORE_N1 = sku("D498-1ECA-87C1", "Spot Preemptible N1 Predefined Instance Core running in "
                   "Americas", family="Compute", unit="h", tiers=[(0, money("0", 18960000))],
                   regions=("us-central1", "us-east1", "us-west1"))
SPOT_RAM_N1 = sku("5451-0A15-0123", "Spot Preemptible N1 Predefined Instance Ram running in "
                  "Americas", family="Compute", unit="GiBy.h", tiers=[(0, money("0", 2541000))],
                  regions=("us-central1", "us-east1", "us-west1"))
GPU_T4 = sku("88B8-C3ED-03F0", "Nvidia Tesla T4 GPU running in Americas", family="Compute",
             group="GPU", unit="h", tiers=[(0, money("0", 350000000))], regions=("us-central1",))
SPOT_GPU_T4 = sku("1A25-07A3-AB6D", "Nvidia Tesla T4 GPU attached to Spot Preemptible VMs running "
                  "in Americas", family="Compute", group="GPU", unit="h",
                  tiers=[(0, money("0", 140000000))], regions=("us-central1",))
STATIC_IP = sku("66A2-68EA-56BE", "Static Ip Charge", family="Network", group="IpAddress",
                unit="h", tiers=[(0, money("0", 0)), (1, money("0", 10000000))],
                regions=("us-central1", "us-east1"))
ALL_VM = [CORE_N1, RAM_N1, SPOT_CORE_N1, SPOT_RAM_N1, GPU_T4, SPOT_GPU_T4]


def n1_vm(**extra) -> model.Resource:
    return resource("Instance", "worker", location="us-central1-c", cpus=4, memory_mb=15360,
                    extra={"machine_type": "n1-standard-4", **extra})


@pytest.mark.parametrize("model_name", ["SPOT", "PREEMPTIBLE"])
def test_a_spot_vm_prices_on_the_spot_records_and_never_the_on_demand_ones(model_name):
    money_out, refusal = price(n1_vm(provisioning_model=model_name), load(ALL_VM, []))
    assert refusal is None
    assert money_out.sku == "5451-0A15-0123+D498-1ECA-87C1"
    assert money_out.usd_per_month == (Decimal("0.01896") * 4 + Decimal("0.002541") * 15) * 730


def test_a_gpu_vm_adds_its_gpus_on_the_on_demand_gpu_record():
    money_out, refusal = price(n1_vm(gpus="2", gpu_type="nvidia-tesla-t4"), load(ALL_VM, []))
    assert refusal is None
    assert "88B8-C3ED-03F0" in money_out.sku and "1A25-07A3-AB6D" not in money_out.sku
    on_demand = (Decimal("0.031611") * 4 + Decimal("0.004237") * 15) * 730
    assert money_out.usd_per_month == on_demand + Decimal("0.35") * 2 * 730


def test_a_spot_gpu_vm_takes_the_spot_gpu_record():
    money_out, _ = price(n1_vm(gpus="1", gpu_type="nvidia-tesla-t4", provisioning_model="SPOT"),
                         load(ALL_VM, []))
    assert money_out.sku == "1A25-07A3-AB6D+5451-0A15-0123+D498-1ECA-87C1"


@pytest.mark.parametrize("extra,phrase", [
    ({"gpus": "1", "gpu_type": "nvidia-h100-80gb"}, "GPU nvidia-h100-80gb not priced here"),
    ({"gpus": "2", "gpu_type": "nvidia-tesla-t4,nvidia-tesla-p4"}, "one type per VM"),
    ({"gpus": "1", "gpu_type": "nvidia-tesla-p4"}, "no dated Nvidia Tesla P4 GPU rate"),
])
def test_a_gpu_the_sheet_cannot_price_refuses_the_whole_row(extra, phrase):
    money_out, refusal = price(n1_vm(**extra), load(ALL_VM, []))
    assert money_out is None and refusal.state == model.UNPRICED and phrase in refusal.reason


def test_a_custom_e2_spot_vm_refuses_because_no_spot_custom_record_is_listed():
    vm = resource("Instance", "w", location="us-central1-c", cpus=2, memory_mb=4096,
                  extra={"machine_type": "e2-custom-2-4096", "provisioning_model": "SPOT"})
    _money, refusal = price(vm, load(ALL_VM, []))
    assert refusal.state == model.UNPRICED and "Spot custom-machine record" in refusal.reason


def test_an_unattached_ip_prices_its_hours_on_the_static_ip_ladder():
    ip = model.Resource(project="p", service="compute.googleapis.com", kind="Address",
                        name="idle", location="us-central1", state="RESERVED")
    money_out, refusal = price(ip, load([STATIC_IP], []))
    assert refusal is None and money_out.sku == "66A2-68EA-56BE"
    assert money_out.usd_per_month == Decimal("0.01") * 729        # the first hour is free


@pytest.mark.parametrize("state,location,phrase", [
    ("IN_USE", "us-central1", "in use"),
    ("RESERVING", "us-central1", "status RESERVING"),
    ("RESERVED", "global", "no dated Static Ip Charge rate for global"),
])
def test_an_address_that_is_not_an_unattached_regional_ip_is_not_priced(state, location, phrase):
    ip = model.Resource(project="p", service="compute.googleapis.com", kind="Address",
                        name="x", location=location, state=state)
    _money, refusal = price(ip, load([STATIC_IP], []))
    assert refusal.state == model.UNPRICED and phrase in refusal.reason
