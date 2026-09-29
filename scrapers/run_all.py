"""Run every registered brand scraper and write canonical output to data/."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.core.writer import write_brand  # noqa: E402

REGISTRY = [
    ("byd", "scrapers.byd.scraper", "fetch"),
    ("xiaomi", "scrapers.xiaomi.scraper", "fetch"),
]


def main():
    for key, module, fn in REGISTRY:
        mod = __import__(module, fromlist=[fn])
        records = getattr(mod, fn)()
        out = write_brand(key, records)
        print(f"OK: {key} -> {len(records)} records -> {out}")


if __name__ == "__main__":
    main()
