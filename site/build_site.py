"""Merge canonical brand JSON into docs/data.json without recall-specific handling."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.core.records import RecordDataError, load_json_records, require_vehicle_records
from scrapers.core.transactions import TransactionSession

DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def build() -> int:
    with TransactionSession(ROOT):
        records = []
        for path in sorted(DATA_DIR.glob("*.json")):
            records.extend(load_json_records(path))
        require_vehicle_records(records)
        content = (json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        _atomic_write(DOCS_DIR / "data.json", content)
    return len(records)


if __name__ == "__main__":
    try:
        print(f"OK: {build()} records -> docs/data.json")
    except (RecordDataError, OSError, ValueError) as exc:
        print(f"ERROR site build: {exc}")
        raise SystemExit(1)
