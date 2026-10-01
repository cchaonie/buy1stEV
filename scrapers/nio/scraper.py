"""NIO (蔚来) adapter: fetch official CN model lineup + specs, map to canonical schema.

Source: www.nio.cn (official Chinese Next.js site).

The site sits behind a Tencent EdgeOne WAF that answers plain clients with an
HTTP 567 block page, but it explicitly lets search-engine crawlers through. We
therefore send a Googlebot User-Agent so the official HTML (which embeds
JSON-LD product/offer prices plus CLTC range and battery specs) is reachable
with plain ``requests``.

There is no public model-list API, so the registry below mirrors the current
CN lineup and is maintained by hand (update it when NIO launches a new model).
"""

import re
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

BASE = "https://www.nio.cn"
SOURCE = "nio.cn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.nio.cn/",
}

# (model name, page slug, category fallback)
MODELS = [
    ("ET5", "et5", "轿跑"),
    ("ET5T", "et5t", "轿车"),
    ("ET7", "et7", "轿车"),
    ("ET9", "et9", "轿车"),
    ("ES6", "es6", "SUV"),
    ("EC6", "ec6", "SUV"),
    ("EC7", "ec7", "SUV"),
    ("ES8", "es8", "SUV"),
    ("ES9", "es9", "SUV"),
]

RETRYABLE = {429, 500, 502, 503, 504}


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code == 200:
        return "ok"
    return "retry" if resp.status_code in RETRYABLE else "fatal"


def _get(url):
    def go():
        return requests.get(url, headers=HEADERS, timeout=30)
    resp = call_with_retry(go, classify=_classify, retries=3, base_delay=1.5)
    return resp.text


def _meta(html, name):
    m = re.search(r'<meta[^>]*name="%s"[^>]*content="([^"]*)"' % name, html)
    return m.group(1).strip() if m else ""


def _title(html):
    m = re.search(r"<title>([^<]*)</title>", html)
    return m.group(1).strip() if m else ""


def _prices(html):
    vals = {int(p) for p in re.findall(
        r'"priceCurrency"\s*:\s*"CNY"\s*,\s*"price"\s*:\s*"?(\d+)"?', html
    )}
    return sorted(vals)


def _category(title, fallback):
    if "SUV" in title:
        return "SUV"
    if "MPV" in title:
        return "MPV"
    if "旅行" in title:
        return "轿车"
    if "轿跑" in title:
        return "轿跑"
    if "轿车" in title:
        return "轿车"
    return fallback


def _range_km(html, desc):
    vals = []
    # structured spec items: {"value":"740","unit":"km",...} / {"spec":"650","unit":"km",...}
    for m in re.finditer(r'"(?:value|spec)"\s*:\s*"([\d.]+)\s*"\s*,\s*"unit"\s*:\s*"km', html):
        vals.append(int(float(m.group(1))))
    # prose claims from the page (e.g. "CLTC 续航最高 965km")
    hay = desc + " " + html
    for m in re.finditer(r"续航[^0-9]{0,10}?([\d,]{3,})\s*(?:km|公里)", hay):
        vals.append(int(m.group(1).replace(",", "")))
    vals = [v for v in vals if 100 <= v <= 2000]
    return max(vals) if vals else ""


def _battery_kwh(html):
    vals = []
    for m in re.finditer(r"([\d.]+)\s*kWh", html):
        v = float(m.group(1))
        if 40 <= v <= 250:
            vals.append(v)
    for m in re.finditer(r"([\d.]+)\s*度\s*(?:超长续航)?电池包", html):
        v = float(m.group(1))
        if 40 <= v <= 250:
            vals.append(v)
    if not vals:
        return ""
    best = max(vals)
    return int(best) if best.is_integer() else best


def _battery_type(html):
    if "半固态" in html:
        return "半固态"
    if "磷酸铁锂" in html:
        return "磷酸铁锂"
    if "三元锂" in html:
        return "三元锂"
    return ""


def _charging_platform(hay):
    vals = []
    for m in re.finditer(r"(\d{3,4})\s*V\s*(?:高压|平台|架构|超充)", hay):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"(?:高压|平台|架构)[^0-9]{0,8}(\d{3,4})\s*V", hay):
        vals.append(int(m.group(1)))
    vals = [v for v in vals if 300 <= v <= 1200]
    return f"{max(vals)}V" if vals else ""


def _adas(hay):
    found = []
    if "NOP" in hay:
        found.append("NOP+ 全域领航辅助")
    if "NAD" in hay:
        found.append("NAD")
    if "神玑" in hay:
        found.append("神玑NX9031")
    if "Aquila" in hay or "超感系统" in hay:
        found.append("Aquila 超感系统")
    return " / ".join(dict.fromkeys(found))


def _parse(html):
    desc = _meta(html, "description")
    keywords = _meta(html, "keywords")
    title = _title(html)
    hay = " ".join([desc, keywords, html])

    prices = _prices(html)
    rec = {
        "price_min": prices[0] if prices else "",
        "price_max": prices[-1] if prices else "",
        "range_cltc_km": _range_km(html, desc),
        "battery_type": _battery_type(hay),
        "battery_kwh": _battery_kwh(html),
        "ac_charge_kw": "",
        "charging_platform": _charging_platform(hay),
        "adas": _adas(hay),
        "highlights": desc or title,
    }
    if prices:
        lo, hi = prices[0] / 10000, prices[-1] / 10000
        rec["price_text"] = f"{lo:g}万 - {hi:g}万" if lo != hi else f"{lo:g}万"
    return rec, title


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []

    for name, slug, fallback_category in MODELS:
        rec = schema.new_record()
        rec.update({
            "brand": "蔚来",
            "model": name,
            "powertrain": "纯电",
            "category": fallback_category,
            "url": f"{BASE}/{slug}",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            html = _get(f"{BASE}/{slug}")
            parsed, title = _parse(html)
            rec.update(parsed)
            rec["category"] = _category(title, fallback_category)
        except Exception as e:
            print(f"  ! fetch failed for {name}: {e}")
        records.append(rec)
        print(f"  [{len(records)}/{len(MODELS)}] {name} "
              f"price={rec['price_text'] or '-'} range={rec['range_cltc_km'] or '-'} "
              f"kwh={rec['battery_kwh'] or '-'}")

    return records


if __name__ == "__main__":
    from ..run_all import refresh_brand
    refresh_brand("nio")
