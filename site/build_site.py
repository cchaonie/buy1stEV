"""Merge all data/*.json into docs/data.json for the static viewer."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"


def build():
    records = []
    for f in sorted(DATA_DIR.glob("*.json")):
        records.extend(json.loads(f.read_text(encoding="utf-8")))
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "data.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(records)


if __name__ == "__main__":
    n = build()
    print(f"OK: {n} records -> docs/data.json")
