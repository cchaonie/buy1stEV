"""XPeng (小鹏) adapter: scrape the official model config tables.

Source: www.xiaopeng.com (Next.js app). Every model page has a companion
"/{slug}/configuration.html" page whose React Server Component flight payload
embeds a `configdata` JSON array (editions -> spec sections -> rows) carrying the
official guide prices and full specifications. The model page marketing blocks
supply the positioning highlights.

There is no public model-list API, so the registry below is maintained by hand
(update it when XPeng launches a new model).
"""

import json
import re
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaopeng.com/",
}
SOURCE = "xiaopeng.com"
BASE = "https://www.xiaopeng.com"

# (display name, page slug, category)
MODELS = [
    ("G6", "g6_2026", "SUV"),
    ("G9", "g9_2026", "SUV"),
    ("G9L", "g9l", "SUV"),
    ("P7", "p7n", "轿车"),
    ("P7+", "p7_plus_2026", "轿车"),
    ("MONA M03", "m03_2026", "轿车"),
    ("X9", "x9_2026", "MPV"),
]

_TAG = re.compile(r"<[^>]+>")
_SUP = re.compile(r"<sup\b[^>]*>.*?</sup>", re.S)
_CONFIG_KEY = '"configdata":'
_GENERIC = ("车长", "车宽", "车高", "轴距", "纯电", "超级增程", "增程式")


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code != 200:
        return "retry" if resp.status_code >= 500 or resp.status_code == 429 else "fatal"
    return "ok"


def _get(url):
    def go():
        return requests.get(url, headers=HEADERS, timeout=30)

    return call_with_retry(go, classify=_classify, retries=3, base_delay=1.0).text


def _clean(text):
    return re.sub(r"\s+", " ", _TAG.sub("", _SUP.sub("", str(text)))).strip()


def _decode_rsc(html):
    """Concatenate the decoded Next.js RSC flight payloads embedded in the HTML."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    out = []
    for p in parts:
        try:
            out.append(json.loads('"' + p + '"'))
        except (ValueError, TypeError):
            continue
    return "".join(out)


def _extract_configdata(html):
    """Return the JSON value that follows `"configdata":` using bracket matching."""
    buf = _decode_rsc(html)
    i = buf.find(_CONFIG_KEY)
    if i < 0:
        return None
    j = i + len(_CONFIG_KEY)
    while j < len(buf) and buf[j] in " \n\t":
        j += 1
    depth = 0
    in_str = False
    esc = False
    start = j
    for k in range(j, len(buf)):
        c = buf[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return json.loads(buf[start:k + 1])
    return None


def _nums(values):
    out = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
        elif isinstance(v, str):
            for m in re.findall(r"\d+(?:\.\d+)?", v):
                out.append(float(m))
    return out


def _powertrain(text):
    if any(k in text for k in ("增程", "REEV", "PHEV", "插混")):
        return "增程"
    if any(k in text for k in ("纯电", "BEV", "EV")):
        return "纯电"
    return ""


def _iter_rows(edition):
    for section in edition.get("data", []):
        for row in section.get("data", []):
            name = _clean(row.get("name", ""))
            if name:
                yield name, row.get("data") or []


def _collect(cd):
    agg = {
        "prices": [],
        "powertrain": set(),
        "battery_type": set(),
        "kwh": [],
        "range_ev": [],
        "range_other": [],
        "platform": set(),
        "ac": [],
        "adas_ngp": False,
        "adas_models": set(),
        "chips": False,
    }
    for edition in cd:
        for p in edition.get("priceConfig", {}).get("data", []):
            if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                agg["prices"].append(p)
        for name, data in _iter_rows(edition):
            joined = " ".join(str(x) for x in data)
            if "能源类型" in name or "能源模式" in name:
                for v in data:
                    pt = _powertrain(str(v))
                    if pt:
                        agg["powertrain"].add(pt)
            if "电池类型" in name:
                for kw in ("磷酸铁锂", "三元锂"):
                    if kw in joined:
                        agg["battery_type"].add(kw)
            if ("电池能量" in name or "电池容量" in name) and "kWh" in name:
                agg["kwh"].extend(_nums(data))
            if "CLTC" in name and "续航" in name:
                if "纯电" in name:
                    agg["range_ev"].extend(_nums(data))
                else:
                    agg["range_other"].extend(_nums(data))
            if "800V" in name:
                agg["platform"].add("800V")
            elif "400V" in name:
                agg["platform"].add("400V")
            if "交流" in name and "功率" in name:
                agg["ac"].extend(_nums(data))
            if "图灵AI芯片" in name:
                agg["chips"] = True
            if "AI辅助驾驶模型" in name:
                for v in data:
                    s = _clean(v)
                    if s and s not in ("-", "●"):
                        agg["adas_models"].add(s)
            if "小鹏图灵AI智驾" in name or ("NGP" in name and "智驾" in name):
                agg["adas_ngp"] = True
    return agg


def _build_fields(agg):
    prices = agg["prices"]
    ranges = agg["range_ev"] or agg["range_other"]
    powertrain = " / ".join(sorted(agg["powertrain"])) or "纯电"

    adas = []
    if agg["adas_ngp"]:
        adas.append("小鹏图灵AI智驾（NGP）")
    adas.extend(sorted(agg["adas_models"]))
    if agg["chips"]:
        adas.append("图灵AI芯片")

    return {
        "powertrain": powertrain,
        "price_min": int(min(prices)) if prices else "",
        "price_max": int(max(prices)) if prices else "",
        "range_cltc_km": int(max(ranges)) if ranges else "",
        "battery_type": " / ".join(sorted(agg["battery_type"])),
        "battery_kwh": max(agg["kwh"]) if agg["kwh"] else "",
        "ac_charge_kw": max(agg["ac"]) if agg["ac"] else "",
        "charging_platform": " / ".join(sorted(agg["platform"])),
        "adas": "；".join(adas),
    }


def _platform_from_text(html):
    buf = _decode_rsc(html)
    if "800V" in buf:
        return "800V"
    if "400V" in buf:
        return "400V"
    return ""


def _highlights(html):
    buf = _decode_rsc(html)
    items = re.findall(r'"header":\{"title":"(.*?)"', buf)
    items += re.findall(r'"line1":"(.*?)"', buf)
    out = []
    for item in items:
        text = _clean(item).replace("\u200b", "").strip()
        if not 2 <= len(text) <= 30 or text in out or text in _GENERIC:
            continue
        if not re.search(r"\d{2,}", text) and text.endswith(("最高", "低至", "至高")):
            continue
        out.append(text)
        if len(out) >= 8:
            break
    return "；".join(out)


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []
    total = len(MODELS)
    for idx, (name, slug, category) in enumerate(MODELS, 1):
        rec = schema.new_record()
        rec.update({
            "brand": "小鹏",
            "model": name,
            "category": category,
            "url": f"{BASE}/{slug}.html",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            config_html = _get(f"{BASE}/{slug}/configuration.html")
            configdata = _extract_configdata(config_html)
            if not configdata:
                raise ValueError("configdata not found in configuration page")
            rec.update(_build_fields(_collect(configdata)))

            model_html = _get(f"{BASE}/{slug}.html")
            highlights = _highlights(model_html)
            if highlights:
                rec["highlights"] = highlights
            if not rec["charging_platform"]:
                rec["charging_platform"] = _platform_from_text(model_html)

            if rec["price_min"] != "":
                rec["price_text"] = (
                    f"{rec['price_min'] / 10000:g}万 - {rec['price_max'] / 10000:g}万"
                )
        except Exception as e:
            print(f"  ! fetch failed for {name}: {e}")
        records.append(rec)
        print(f"  [{idx}/{total}] {name} ({category}) price={rec['price_text'] or 'n/a'}")
    return records


if __name__ == "__main__":
    from ..core.writer import write_brand

    write_brand("xpeng", fetch())
