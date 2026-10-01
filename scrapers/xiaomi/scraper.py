"""Xiaomi EV adapter: fetch official version list per model, map to canonical schema.

Source: website-api.xiaomiev.com (open POST, no auth).
There is no model-list API, so the model registry below is maintained by hand
(update it when Xiaomi launches a new model).
"""

import re
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

API = "https://website-api.xiaomiev.com/mtop/guidemarketing/product/car/versionList"
HEADERS = {
    "Content-Type": "application/json",
    "x-user-agent": "channel/car platform/car.pc",
    "Origin": "https://www.xiaomiev.com",
    "Referer": "https://www.xiaomiev.com/",
}
SOURCE = "xiaomiev.com"

# (display name, goodsId, category, powertrain, model page path)
MODELS = [
    ("SU7", "900010301", "轿车", "纯电", "/su7"),
    ("YU7", "500015457", "SUV", "纯电", "/yu7"),
    ("SU7 Ultra", "500010402", "轿跑", "纯电", "/xiaomi/ultra"),
    ("N70", "900020768", "SUV", "增程", "/skynomad/n70"),
    ("N90 Max", "900020450", "SUV", "增程", "/skynomad/n90"),
]


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code != 200:
        return "retry" if resp.status_code >= 500 or resp.status_code == 429 else "fatal"
    return "ok"


def _version_list(goods_id):
    def go():
        return requests.post(
            API, headers=HEADERS,
            json=[{"goodsId": goods_id, "inventoryChannel": "NORMAL", "detailMode": 1}],
            timeout=30,
        )
    resp = call_with_retry(go, classify=_classify, retries=3, base_delay=1.0)
    return resp.json().get("data", {}).get("detail", {}) or {}


def _find(pattern, text):
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _parse(ssu_list):
    prices, ranges, kwhs, volts, types, points, adas = [], [], [], [], set(), [], set()
    for v in ssu_list:
        mp = v.get("marketPrice")
        if mp:
            prices.append(mp / 100)
        pts = " ".join(p.get("payload", "") for p in (v.get("richTextSellingPoints") or []))
        for a in v.get("attributeList", []):
            name = a.get("attributeName", "")
            val = str(a.get("attributeValueName", ""))
            if "续航里程(CLTC)" in name and val.isdigit():
                ranges.append(int(val))
            if "辅助驾驶" in name and val:
                adas.add(val)
        if "磷酸铁锂" in pts:
            types.add("磷酸铁锂")
        if "三元锂" in pts:
            types.add("三元锂")
        kwh = _find(r"(\d+\.?\d*)\s*kWh", pts)
        if kwh:
            kwhs.append(float(kwh))
        volt = _find(r"(\d{3,4})\s*V", pts)
        if volt:
            volts.append(int(volt))
        ev_range = _find(r"纯电续航\s*(\d+)\s*km", pts)
        if ev_range:
            ranges.append(int(ev_range))
        points.extend(p.get("payload", "") for p in (v.get("richTextSellingPoints") or []))

    uniq_points = [p for i, p in enumerate(points) if p and p not in points[:i]]
    return {
        "price_min": int(min(prices)) if prices else "",
        "price_max": int(max(prices)) if prices else "",
        "range_cltc_km": max(ranges) if ranges else "",
        "battery_type": " / ".join(sorted(types)),
        "battery_kwh": max(kwhs) if kwhs else "",
        "charging_platform": f"{max(volts)}V" if volts else "",
        "adas": " / ".join(sorted(adas)),
        "highlights": "；".join(uniq_points[:8]),
    }


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []
    for name, goods_id, category, powertrain, path in MODELS:
        rec = schema.new_record()
        rec.update({
            "brand": "小米",
            "model": name,
            "category": category,
            "powertrain": powertrain,
            "url": f"https://www.xiaomiev.com{path}",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            detail = _version_list(goods_id)
            rec.update(_parse(detail.get("ssuList", [])))
        except Exception as e:
            print(f"  ! fetch failed for {name}: {e}")
        if rec["price_min"] != "":
            rec["price_text"] = f"{rec['price_min']/10000:g}万 - {rec['price_max']/10000:g}万"
        records.append(rec)
        print(f"  [{len(records)}/{len(MODELS)}] {name}")

    return records


if __name__ == "__main__":
    from ..run_all import refresh_brand
    refresh_brand("xiaomi")
