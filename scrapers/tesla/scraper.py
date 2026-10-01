"""Tesla China (特斯拉) adapter: parse the official configurator payload.

Source: https://www.tesla.cn/<model>/design (server-rendered).
Each design page embeds a large ``const dataJson = {...}`` JS object. Its
``DSServices["Lexicon.<product>"]`` entry lists every ``options`` item; the
items whose ``group`` is ``"TRIM"`` are the currently sold trims with CNY
prices and ``lexicon_specs`` (CLTC range / acceleration / top speed).

One canonical record is emitted per model line, aggregating price and range
across that line's trims (same approach as the Xiaomi adapter).
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
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tesla.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
SOURCE = "tesla.cn"

# (lexicon product key, display name, category, design page path)
MODELS = [
    ("m3", "Model 3", "轿车", "/model3/design"),
    ("my", "Model Y", "SUV", "/modely/design"),
]

BATTERY_TYPES = {"lfp": "磷酸铁锂", "ternary_nmc": "三元锂"}
ADAS_HW = {"ai_4": "HW4.0", "ai_3": "HW3.0"}
CHARGE_KW_RE = re.compile(r"charge_(\d+)_kw")


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code != 200:
        return "retry" if resp.status_code >= 500 or resp.status_code == 429 else "fatal"
    return "ok"


def _get(url):
    def go():
        return requests.get(url, headers=HEADERS, timeout=30)

    return call_with_retry(go, classify=_classify, retries=3, base_delay=1.0)


def _extract_js_object(html, marker):
    """Return the balanced ``{...}`` literal following ``marker`` in ``html``."""
    i = html.find(marker)
    if i < 0:
        raise ValueError(f"marker not found: {marker!r}")
    start = html.find("{", i)
    if start < 0:
        raise ValueError(f"no object after marker: {marker!r}")
    depth, in_str, esc = 0, False, False
    for k in range(start, len(html)):
        c = html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start:k + 1]
    raise ValueError("unbalanced JS object")


def _load_datajson(html):
    blob = _extract_js_object(html, "const dataJson")
    # dataJson is a JS object literal: strip trailing commas so json can parse it.
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", blob))


def _lease(lex):
    """Return the list of currently sold TRIM option objects."""
    trims = []
    for opt in (lex.get("options") or {}).values():
        if opt.get("group") != "TRIM":
            continue
        if not isinstance(opt.get("price"), (int, float)):
            continue
        trims.append(opt)
    return trims


def _drive(name):
    if "全轮驱动" in name:
        return "全轮驱动"
    if "后轮驱动" in name:
        return "后轮驱动"
    return ""


def _parse(lex, model_name):
    prices, ranges, batteries, hw, charge_kws, highlights = [], [], set(), set(), [], []
    for trim in _lease(lex):
        name = trim.get("name") or ""
        price = trim.get("price")
        prices.append(int(price))

        specs = trim.get("lexicon_specs") or {}
        raw = specs.get("rawData") or {}
        data = raw.get("data") or {}

        rng = specs.get("range")
        acc = specs.get("acceleration")
        if isinstance(rng, (int, float)):
            ranges.append(int(rng))

        bat = (data.get("battery_type") or {}).get("feature_option")
        if bat in BATTERY_TYPES:
            batteries.add(BATTERY_TYPES[bat])

        charge = (data.get("max_charge_power") or {}).get("feature_option") or ""
        m = CHARGE_KW_RE.search(charge)
        if m:
            charge_kws.append(int(m.group(1)))

        hw_code = (data.get("driver_assistance") or {}).get("feature_option")
        if hw_code:
            hw.add(hw_code)

        short = name.replace(model_name + " ", "").strip() or name
        bits = []
        if isinstance(rng, (int, float)):
            bits.append(f"CLTC {int(rng)}km")
        if isinstance(acc, (int, float)):
            bits.append(f"0-100km/h {acc:g}秒")
        drive = _drive(name)
        if drive:
            bits.append(drive)
        if (data.get("seating_capacity") or {}).get("feature_option") == "six_seat":
            bits.append("六座")
        highlights.append(f"{short}：" + "，".join(bits))

    uniq_highlights = [h for i, h in enumerate(highlights) if h not in highlights[:i]]
    label = " / ".join(ADAS_HW[c] for c in sorted(hw) if c in ADAS_HW)

    battery_type = " / ".join(sorted(batteries))
    max_charge = max(charge_kws) if charge_kws else None
    rec = {
        "price_min": min(prices) if prices else "",
        "price_max": max(prices) if prices else "",
        "range_cltc_km": max(ranges) if ranges else "",
        "battery_type": battery_type,
        "charging_platform": "400V" if max_charge else "",
        "adas": f"基础辅助驾驶（{label}）" if label else "",
        "highlights": "；".join(uniq_highlights[:8]),
    }
    if max_charge:
        rec["highlights"] += ("；" if rec["highlights"] else "") + f"最高充电功率{max_charge}kW"
    return rec


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    records = []
    for key, name, category, path in MODELS:
        rec = schema.new_record()
        rec.update({
            "brand": "特斯拉",
            "model": name,
            "powertrain": "纯电",
            "category": category,
            "url": f"https://www.tesla.cn{path}",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            html = _get(rec["url"]).text
            data = _load_datajson(html)
            lex = data["DSServices"][f"Lexicon.{key}"]
            rec.update(_parse(lex, name))
        except Exception as e:
            print(f"  ! fetch failed for {name}: {e}")
        if rec["price_min"] != "":
            rec["price_text"] = (
                f"{rec['price_min'] / 10000:g}万 - {rec['price_max'] / 10000:g}万"
            )
        records.append(rec)
        print(f"  [{len(records)}/{len(MODELS)}] {name}")

    return records


if __name__ == "__main__":
    from ..run_all import refresh_brand
    refresh_brand("tesla")
