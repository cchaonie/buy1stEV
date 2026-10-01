"""Shared validation for canonical vehicle records."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import schema


class RecordDataError(ValueError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordDataError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json_records(path: Path) -> list[dict]:
    path = Path(path)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise RecordDataError(f"{path}: exceeds 5 MiB")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RecordDataError(f"{path}: non-finite constant {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordDataError(f"{path}: invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, list):
        raise RecordDataError(f"{path}: records must be an array")
    return value


def validate_vehicle_records(records: object) -> list[str]:
    if not isinstance(records, list):
        return ["records: expected array"]
    errors: list[str] = []
    expected = set(schema.FIELDS)
    numeric = {"range_cltc_km", "battery_kwh", "ac_charge_kw"}
    prices = {"price_min", "price_max"}
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        path = f"records[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{path}: expected object")
            continue
        if set(record) != expected:
            errors.append(f"{path}: field set does not match schema")
            continue
        brand, model = record["brand"], record["model"]
        for field, value in (("brand", brand), ("model", model)):
            if not isinstance(value, str) or not value or value.strip() != value:
                errors.append(f"{path}.{field}: expected non-empty trimmed string")
        if isinstance(brand, str) and isinstance(model, str):
            key = (brand, model)
            if key in seen:
                errors.append(f"{path}: duplicate vehicle key {key}")
            seen.add(key)
        for field, value in record.items():
            if field in {"brand", "model"}:
                continue
            if field in prices:
                if value != "" and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    errors.append(f"{path}.{field}: expected non-negative integer or empty string")
            elif field in numeric:
                number_text = isinstance(value, str) and bool(re.fullmatch(r"\d+(?:\.\d+)?", value))
                if value != "" and not number_text and (
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                ):
                    errors.append(f"{path}.{field}: expected finite non-negative number, numeric string, or empty string")
            elif not isinstance(value, str):
                errors.append(f"{path}.{field}: expected string")
    return errors


def require_vehicle_records(records: object) -> None:
    errors = validate_vehicle_records(records)
    if errors:
        raise RecordDataError("\n".join(errors))
