"""AITO (问界) adapter: fetch official price/spec tables, map to canonical schema.

Source: aito.auto (Adobe Experience Manager static site).

Each model has a marketing page under /model/<slug>/ and a full parameter/spec
table under /model/<slug>/configuration/. Trim prices sit in the static
"建议零售价" row of the spec table as currency amounts ("¥ 489,800", or the
full-width "￥239,800" used on the new M5 page); all trims are folded into a
single price_min/price_max range per model. Range, battery and charge specs are
read from the same spec table, and the marketing page supplies the highlights.

There is no public model-list API, so the registry below mirrors the current
CN lineup and is maintained by hand (update it when AITO launches a model).
"""

import re
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

BASE = "https://aito.auto"
SOURCE = "aito.auto"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
RETRYABLE = {429, 500, 502, 503, 504}

# (display name, page slug, category) -- all current AITO models are SUVs
MODELS = [
    ("M5", "m5-new", "SUV"),
    ("M6", "m6", "SUV"),
    ("M7", "m7-new", "SUV"),
    ("M8", "m8", "SUV"),
    ("M9", "m9-new", "SUV"),
]


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code == 200:
        return "ok"
    return "retry" if resp.status_code in RETRYABLE else "fatal"


def _get(url):
    def go():
        return requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    resp = call_with_retry(go, classify=_classify, retries=3, base_delay=1.5)
    return resp.text


def _meta(html, name):
    m = re.search(r'<meta[^>]*name="%s"[^>]*content="([^"]*)"' % name, html)
    return m.group(1).strip() if m else ""


def _text(html):
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", body)
    text = text.replace("\u200c", "").replace("\u2060", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text)


def _prices(html):
    """Union the currency amounts in every "建议零售价" row (all trim groups)."""
    vals = set()
    for m in re.finditer(r"建议零[\s\S]{0,120}?价", html):
        end = html.find('<div class="line', m.end() + 1)
        seg = html[m.end():end if end > 0 else m.end() + 20000]
        for x in re.findall(r"[¥￥]\s*([\d,]{5,})", seg):
            vals.add(int(x.replace(",", "")))
    return sorted(vals)


def _range_km(text):
    vals = []
    for m in re.finditer(r"CLTC", text):
        seg = text[m.start():m.start() + 240]
        vals.extend(int(n) for n in re.findall(r"\d{3,4}", seg) if 150 <= int(n) <= 2000)
    return max(vals) if vals else ""


def _battery_kwh(text):
    vals = []
    for m in re.finditer(r"电池\s*容量", text):
        seg = text[m.start():m.start() + 160]
        hits = re.findall(r"(\d+(?:\.\d+)?)\s*kWh", seg)
        if hits:
            vals.extend(float(x) for x in hits)
            continue
        run = re.search(r"电池\s*容量[^0-9]{0,12}((?:\d+(?:\.\d+)?[\s、，,]*)+)", seg)
        if run:
            vals.extend(float(x) for x in re.findall(r"\d+(?:\.\d+)?", run.group(1))
                        if 30 <= float(x) <= 200)
    if not vals:
        return ""
    best = max(vals)
    return int(best) if best.is_integer() else best


def _battery_type(text):
    found = [t for t in ("半固态", "磷酸铁锂", "三元锂") if t in text]
    return " / ".join(found)


def _charging_platform(text):
    vals = [int(x) for x in re.findall(r"(\d{3})\s*V", text) if 300 <= int(x) <= 1200]
    return f"{max(vals)}V" if vals else ""


def _ac_charge_kw(text):
    patterns = (
        r"约?\s*(\d+(?:\.\d+)?)\s*kW[^。；]{0,25}?交流",
        r"交流[^。；]{0,25}?(\d+(?:\.\d+)?)\s*kW",
        r"慢充[^。；]{0,60}?(\d+(?:\.\d+)?)\s*kW",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if m and 3 <= float(m.group(1)) <= 22:
            return m.group(1)
    return ""


def _powertrain(text):
    found = [p for p in ("增程", "纯电", "插混") if p in text]
    return "/".join(found)


def _highlights(desc, title):
    source = desc or title
    parts = [p.strip(" 。") for p in re.split(r"[，,]", source) if p.strip(" 。")]
    return "；".join(parts)


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []

    for name, slug, category in MODELS:
        rec = schema.new_record()
        rec.update({
            "brand": "问界",
            "model": name,
            "category": category,
            "url": f"{BASE}/model/{slug}/",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            conf_html = _get(f"{BASE}/model/{slug}/configuration/")
            text = _text(conf_html)
            model_html = _get(f"{BASE}/model/{slug}/")
            desc = _meta(model_html, "description") or _meta(conf_html, "description")

            prices = _prices(conf_html)
            rec.update({
                "powertrain": _powertrain(text),
                "price_min": prices[0] if prices else "",
                "price_max": prices[-1] if prices else "",
                "range_cltc_km": _range_km(text),
                "battery_type": _battery_type(text),
                "battery_kwh": _battery_kwh(text),
                "ac_charge_kw": _ac_charge_kw(text),
                "charging_platform": _charging_platform(text),
                "adas": "华为ADS" if "ADS" in (desc + text) else "",
                "highlights": _highlights(desc, _meta(conf_html, "title")),
            })
            if prices:
                lo, hi = prices[0] / 10000, prices[-1] / 10000
                rec["price_text"] = f"{lo:g}万 - {hi:g}万" if lo != hi else f"{lo:g}万"
        except Exception as e:
            print(f"  ! fetch failed for {name}: {e}")
        records.append(rec)
        print(f"  [{len(records)}/{len(MODELS)}] {name} "
              f"price={rec['price_text'] or '-'} range={rec['range_cltc_km'] or '-'} "
              f"kwh={rec['battery_kwh'] or '-'}")

    return records


if __name__ == "__main__":
    from ..core.writer import write_brand
    write_brand("aito", fetch())
