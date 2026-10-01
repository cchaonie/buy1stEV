"""Collect brand records, merge audited recall projection, then commit one batch."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.core.records import RecordDataError, require_vehicle_records
from scrapers.core.transactions import TransactionSession
from scrapers.core.writer import write_brand, write_brands
from scrapers.recalls.core import RecallDataError, explicit_target_keys, validate_catalog_targets
from scrapers.recalls.scraper import RecallProjectionRecords


@dataclass(frozen=True)
class RegistryEntry:
    kind: Literal["brand", "enricher"]
    key: str
    display_brand: str | None
    module: str
    function: str


REGISTRY = (
    RegistryEntry("enricher", "recalls", None, "scrapers.recalls.scraper", "fetch"),
    RegistryEntry("brand", "byd", "比亚迪", "scrapers.byd.scraper", "fetch"),
    RegistryEntry("brand", "xiaomi", "小米", "scrapers.xiaomi.scraper", "fetch"),
    RegistryEntry("brand", "tesla", "特斯拉", "scrapers.tesla.scraper", "fetch"),
    RegistryEntry("brand", "nio", "蔚来", "scrapers.nio.scraper", "fetch"),
    RegistryEntry("brand", "xpeng", "小鹏", "scrapers.xpeng.scraper", "fetch"),
    RegistryEntry("brand", "lixiang", "理想", "scrapers.lixiang.scraper", "fetch"),
    RegistryEntry("brand", "zeekr", "极氪", "scrapers.zeekr.scraper", "fetch"),
    RegistryEntry("brand", "aito", "问界", "scrapers.aito.scraper", "fetch"),
)
BRAND_REGISTRY = tuple(entry for entry in REGISTRY if entry.kind == "brand")
BRAND_KEYS = frozenset(entry.key for entry in BRAND_REGISTRY)


def _call(entry: RegistryEntry):
    return getattr(importlib.import_module(entry.module), entry.function)()


def validate_brand_records(entry: RegistryEntry, records: object) -> list[dict]:
    if entry.kind != "brand" or entry.display_brand is None:
        raise RecordDataError(f"{entry.key}: not a brand registry entry")
    require_vehicle_records(records)
    if not records:
        raise RecordDataError(f"{entry.key}: empty record list")
    if any(record["brand"] != entry.display_brand for record in records):
        raise RecordDataError(f"{entry.key}: record brand differs from registry display brand")
    return records


def merge_recall_projection(pending: dict[str, list[dict]], projections: RecallProjectionRecords, required_targets: set[tuple[str, str]]) -> None:
    require_vehicle_records(list(projections))
    if {(item["brand"], item["model"]) for item in projections} != required_targets or len(projections) != len(required_targets):
        raise RecallDataError("recall projection targets differ from catalog explicit targets")
    index = {(row["brand"], row["model"]): row for rows in pending.values() for row in rows}
    for projection in projections:
        key = (projection["brand"], projection["model"])
        if key not in index:
            raise RecallDataError(f"recall projection target does not exist: {key}")
        if any(projection[field] != "" for field in projection if field not in {"brand", "model", "recall_history"}):
            raise RecallDataError(f"recall projection changes non-recall fields: {key}")
        index[key]["recall_history"] = projection["recall_history"]
    for records in pending.values():
        require_vehicle_records(records)


def _recalls() -> RecallProjectionRecords:
    result = _call(REGISTRY[0])
    if not isinstance(result, RecallProjectionRecords):
        raise RecallDataError("recall enricher must return RecallProjectionRecords")
    return result


def collect_batch(entries=BRAND_REGISTRY) -> dict[str, list[dict]]:
    projections = _recalls()
    pending = {entry.key: validate_brand_records(entry, _call(entry)) for entry in entries}
    vehicle_keys = {(row["brand"], row["model"]) for rows in pending.values() for row in rows}
    brands = {entry.display_brand for entry in entries}
    errors = validate_catalog_targets(projections.catalog, vehicle_keys, brands=brands)
    if errors:
        raise RecallDataError(errors)
    required = explicit_target_keys(projections.catalog, brands=brands)
    merge_recall_projection(pending, RecallProjectionRecords([row for row in projections if row["brand"] in brands], projections.catalog), required)
    return pending


def main() -> None:
    with TransactionSession(ROOT) as session:
        pending = collect_batch()
        write_brands(pending, expected_keys=BRAND_KEYS, session=session)
    for key, records in pending.items():
        print(f"OK: {key} -> {len(records)} records -> data/{key}.json")


def refresh_brand(key: str) -> None:
    entry = next((entry for entry in BRAND_REGISTRY if entry.key == key), None)
    if entry is None:
        raise ValueError(f"unknown brand key {key!r}")
    with TransactionSession(ROOT) as session:
        pending = collect_batch((entry,))
        write_brand(key, pending[key], expected_key=key, session=session)
    print(f"OK: {key} -> {len(pending[key])} records -> data/{key}.json")


if __name__ == "__main__":
    try:
        main()
    except (RecallDataError, RecordDataError, OSError, ValueError) as exc:
        print(f"ERROR run_all: {exc}")
        raise SystemExit(1)
