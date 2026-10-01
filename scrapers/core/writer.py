"""Serialize caller-provided canonical records into brand JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Mapping

from . import schema
from .records import require_vehicle_records
from .transactions import TransactionSession, write_bytes_many

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"


def json_bytes(records: list[dict]) -> bytes:
    return (json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def csv_bytes(records: list[dict], fieldnames=None, lineterminator="\n") -> bytes:
    fieldnames = schema.FIELDS if fieldnames is None else fieldnames
    output = io.StringIO(newline="")
    output_writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator=lineterminator)
    output_writer.writeheader()
    output_writer.writerows(records)
    return output.getvalue().encode("utf-8-sig")


def _payloads(records_by_key: Mapping[str, list[dict]], expected_keys: frozenset[str]) -> dict[Path, bytes]:
    if set(records_by_key) != set(expected_keys):
        raise ValueError(f"brand keys must exactly equal authorized keys: expected {sorted(expected_keys)}, got {sorted(records_by_key)}")
    payloads = {}
    for key, records in records_by_key.items():
        require_vehicle_records(records)
        payloads[DATA_DIR / f"{key}.json"] = json_bytes(records)
        payloads[DATA_DIR / f"{key}.csv"] = csv_bytes(records)
    return payloads


def write_brands(records_by_key: Mapping[str, list[dict]], *, expected_keys: frozenset[str], session: TransactionSession | None = None) -> dict[str, Path]:
    """Write exactly a caller-authorized batch; it has no recall-specific behavior."""
    payloads = _payloads(records_by_key, expected_keys)
    if session is None:
        with TransactionSession(REPO_ROOT) as owned:
            write_bytes_many(payloads, owned)
    else:
        write_bytes_many(payloads, session)
    return {key: DATA_DIR / f"{key}.json" for key in records_by_key}


def write_brand(brand_key: str, records: list[dict], *, expected_key: str, session: TransactionSession | None = None) -> Path:
    if brand_key != expected_key:
        raise ValueError(f"unauthorized brand key {brand_key!r}")
    return write_brands({brand_key: records}, expected_keys=frozenset({expected_key}), session=session)[brand_key]
