"""Write canonical records to data/ as JSON and CSV."""

import csv
import json
from pathlib import Path

from . import schema

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def write_brand(brand_key, records):
    DATA_DIR.mkdir(exist_ok=True)
    out_json = DATA_DIR / f"{brand_key}.json"
    out_json.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    if records:
        with (DATA_DIR / f"{brand_key}.csv").open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=schema.FIELDS)
            w.writeheader()
            w.writerows(records)

    return out_json
