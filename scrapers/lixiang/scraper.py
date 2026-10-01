"""Li Auto (理想) adapter: fetch official on-sale lineup, map to canonical schema.

Source: api-web.lixiang.com
  /vehicle-api/v1-0/products/product/all-on-sale/all-series (open GET, no auth)

`all-series` is the authoritative on-sale list: one entry per series (L6, L8,
L9, MEGA, i8, i9) with each trim's `characteristic` highlights and
`unifiedRetailPrice` (in 分 / cents; divide by 100 for 元).  There is no model
list API, but the series list is returned by the endpoint itself, so nothing is
hard-coded here.

Specs that the API does not expose (电池类型, 交流充电功率) are left as "".
"""

import re
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

API = (
    "https://api-web.lixiang.com"
    "/vehicle-api/v1-0/products/product/all-on-sale/all-series"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.lixiang.com",
    "Referer": "https://www.lixiang.com/",
    "X-CHJ-MetaData": '{"language":"zh"}',
}
SOURCE = "lixiang.com"

ADAS_MARKERS = (
    "智驾", "智能驾驶", "辅助驾驶", "AD Max", "AD Pro", "TOPS", "激光雷达",
)


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code == 200:
        return "ok"
    return "retry" if resp.status_code >= 500 or resp.status_code == 429 else "fatal"


def _series_list():
    def go():
        return requests.get(API, headers=HEADERS, timeout=30)

    resp = call_with_retry(go, classify=_classify, retries=3, base_delay=1.0)
    return resp.json().get("data") or []


def _find(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _powertrain(chars_text, car_model):
    if "增程" in chars_text:
        return "增程"
    if car_model.startswith("X"):
        return "增程"
    if car_model.startswith("W"):
        return "纯电"
    return ""


def _range_km(chars_text, powertrain):
    # EREV: CLTC pure-electric range is the EV figure (e.g. L6 300km, L8 430km).
    m = _find(chars_text, r"纯电续航\s*(\d+)")
    if m:
        return int(m)
    # BEV: CLTC combined range equals the EV range.
    m = _find(chars_text, r"CLTC[^0-9]{0,8}(?:综合)?续航[^0-9]{0,4}(\d{3,4})")
    if m:
        return int(m)
    m = _find(chars_text, r"(\d{3,4})\s*[kK][mM]")
    if m:
        return int(m)
    return ""


def _battery_kwh(chars_text):
    m = _find(chars_text, r"(\d+(?:\.\d+)?)\s*千瓦时")
    return float(m) if m else ""


def _charging_platform(chars_text):
    m = _find(chars_text, r"(\d+)C")
    return f"{m}C" if m else ""


def _adas(items):
    found = [p.strip() for p in items if any(m in p for m in ADAS_MARKERS)]
    uniq = [p for i, p in enumerate(found) if p not in found[:i]]
    return " / ".join(uniq)


def _url(model):
    if model.lower().startswith("i") or model.upper() == "MEGA":
        return f"https://www.lixiang.com/{model.lower()}"
    return f"https://www.lixiang.com/{model.upper()}"


def _parse_series(series):
    chars, prices, cars = [], [], []
    for m in series.get("modelList") or []:
        chars.extend(c for c in (m.get("characteristic") or []) if c)
        price = m.get("unifiedRetailPrice")
        if price:
            prices.append(int(price) / 100)
        if m.get("carModel"):
            cars.append(m["carModel"])
        for p in m.get("parameterConfig") or []:
            if p.get("name"):
                chars.append(p["name"])
            if p.get("desc"):
                chars.append(p["desc"])

    uniq_chars = [c.strip() for i, c in enumerate(chars) if c not in chars[:i]]
    text = " ".join(uniq_chars)
    powertrain = _powertrain(text, cars[0] if cars else "")

    return {
        "powertrain": powertrain,
        "category": "MPV" if "MEGA" in series.get("seriesName", "") else "SUV",
        "price_min": int(min(prices)) if prices else "",
        "price_max": int(max(prices)) if prices else "",
        "range_cltc_km": _range_km(text, powertrain),
        "battery_kwh": _battery_kwh(text),
        "charging_platform": _charging_platform(text),
        "adas": _adas(uniq_chars),
        "highlights": "；".join(uniq_chars),
    }


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []

    try:
        series = _series_list()
    except Exception as e:
        print(f"  ! series list failed: {e}")
        return records

    total = len(series)
    for i, s in enumerate(series, 1):
        model = (s.get("seriesName") or "").replace("理想", "").strip()
        rec = schema.new_record()
        rec.update({
            "brand": "理想",
            "model": model,
            "url": _url(model),
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            rec.update(_parse_series(s))
            if rec["price_min"] != "":
                rec["price_text"] = (
                    f"{rec['price_min']/10000:g}万 - {rec['price_max']/10000:g}万"
                )
        except Exception as e:
            print(f"  ! parse failed for {model}: {e}")
        records.append(rec)
        print(f"  [{i}/{total}] {model}  {rec.get('price_text', '')}")

    return records


if __name__ == "__main__":
    from ..run_all import refresh_brand
    refresh_brand("lixiang")
