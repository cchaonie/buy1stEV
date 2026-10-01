"""Recall projection adapter; it returns records and never writes data files."""

from __future__ import annotations

from scrapers.core import schema

from .core import (
    RecallDataError,
    explicit_target_keys,
    expected_items,
    load_catalog,
    require_valid_catalog,
    serialize_items,
)


class RecallProjectionRecords(list):
    """Canonical projection records plus the validated catalog consumed by run_all."""

    def __init__(self, records, catalog):
        super().__init__(records)
        self.catalog = catalog


def fetch() -> list[dict]:
    catalog = load_catalog()
    require_valid_catalog(catalog)
    projection = expected_items(catalog)
    targets = explicit_target_keys(catalog, brands=None)
    records = []
    for brand, model in sorted(targets):
        record = schema.new_record()
        record.update({"brand": brand, "model": model, "recall_history": serialize_items(projection.get((brand, model), []))})
        records.append(record)
    return RecallProjectionRecords(records, catalog)


if __name__ == "__main__":
    try:
        records = fetch()
        print(f"OK: recall projection -> {len(records)} targets")
    except RecallDataError as exc:
        print(f"ERROR recalls: {exc}")
        raise SystemExit(1)
