"""BYD adapter: fetch official model list + specs, map to canonical schema."""

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

CMS_BASE = "https://cms-api.byd.com/car/byd/cn"
SITE_BASE = "https://site-api.byd.com/domestic-official-api"
SIGN_KEY = "4a3688a5gcd88g443fga6b7fcb"
SECRET_KEY = "fcb8f0ddg5c92g45b7g9d33g04cc55d3be3b"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.byd.com/"}
SOURCE = "byd.com"

OK_CODES = {0, 200}
RATE_LIMIT_CODES = {30001}
REQUEST_DELAY = 1.0

CATEGORY_MAP = [
    ("海狮06", "SUV"), ("海狮", "SUV"), ("唐", "SUV"), ("宋", "SUV"), ("元", "SUV"),
    ("腾势D9", "MPV"), ("夏", "MPV"),
    ("驱逐舰", "轿车"), ("海豹06", "轿车"), ("海豹", "轿车"), ("海豚", "轿车"),
    ("海鸥", "轿车"), ("汉", "轿车"), ("秦", "轿车"),
]


def signed_headers(network):
    ts = int(time.time())
    signing = f"{SIGN_KEY}\n{SECRET_KEY}\n{ts}"
    sig = hmac.new(SECRET_KEY.encode(), signing.encode(), hashlib.sha256).hexdigest()
    return {
        "X-HMAC-SIGNATURE": sig,
        "X-HMAC-TIMESTAMP": str(ts),
        "X-HMAC-SIGNKEY": SIGN_KEY,
        "saleNetwork": str(network),
        **HEADERS,
    }


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code != 200:
        return "fatal" if 400 <= resp.status_code < 500 else "retry"
    try:
        code = resp.json().get("code")
    except Exception:
        return "fatal"
    if code in OK_CODES:
        return "ok"
    if code in RATE_LIMIT_CODES:
        return "retry"
    return "fatal"


def _get(url, *, params=None, headers=None, retries=3):
    def go():
        return requests.get(url, params=params, headers=headers or HEADERS, timeout=30)
    return call_with_retry(go, classify=_classify, retries=retries, base_delay=1.0)


def fetch_models():
    resp = _get(f"{CMS_BASE}/goodsListForSearch")
    return resp.json()["data"]


def fetch_params(goods_id, network):
    def go():
        return requests.get(
            f"{SITE_BASE}/goods/goodsParams",
            params={"goodsId": goods_id},
            headers=signed_headers(network),
            timeout=30,
        )
    resp = call_with_retry(go, classify=_classify, retries=4, base_delay=2.0)
    return resp.json().get("data") or {}


def _parse_price(price_str):
    s = price_str.replace(",", "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", s)]
    if not nums:
        return None, None
    if "万" in price_str:
        nums = [n * 10000 for n in nums]
    return int(min(nums)), int(max(nums))


def _flatten(configs):
    fields, sections = {}, []
    for sec in configs or []:
        sections.append(sec["name"])
        for f in sec.get("value") or []:
            vals = [v for v in (f.get("value") or []) if v]
            fields.setdefault(f["name"], []).extend(vals)
    return sections, fields


def _pick_max_number(fields, keywords):
    best = None
    for name, vals in fields.items():
        if not any(k in name for k in keywords):
            continue
        for v in vals:
            for n in re.findall(r"\d+(?:\.\d+)?", str(v)):
                best = float(n) if best is None else max(best, float(n))
    return best


def _battery_type(fields):
    blob = " ".join(fields.keys()) + " " + " ".join(
        " ".join(map(str, v)) for v in fields.values()
    )
    if "三元" in blob:
        return "三元锂"
    if "刀片" in blob or "磷酸铁" in blob:
        return "磷酸铁锂"
    return ""


def _adas(sections, fields):
    joined = " ".join(list(fields.keys()) + sections)
    if "城市领航" in joined or "CNOA" in joined or "城市NOA" in joined:
        return "高阶(城市NOA)"
    if "自适应巡航" in joined or "ACC" in joined:
        return "基础辅助"
    return ""


def _category(name):
    for k, v in CATEGORY_MAP:
        if k in name:
            return v
    return ""


def _parse_specs(data, name):
    configs = data.get("configs") or []
    sections, fields = _flatten(configs)
    all_text = json.dumps(data, ensure_ascii=False)

    if "发动机" in all_text or "燃油" in all_text:
        powertrain = "插混"
    else:
        powertrain = "纯电"

    if powertrain == "纯电":
        cltc = _pick_max_number(fields, ["续航", "续驶"])
    else:
        cltc = _pick_max_number(
            {k: v for k, v in fields.items() if "纯电" in k}, ["续航", "续驶"]
        )

    return {
        "powertrain": powertrain,
        "range_cltc_km": int(cltc) if cltc else "",
        "battery_type": _battery_type(fields),
        "battery_kwh": _pick_max_number(fields, ["电池电量", "电池容量"]) or "",
        "ac_charge_kw": _pick_max_number(fields, ["交流充电"]) or "",
        "adas": _adas(sections, fields),
        "charging_platform": "",
    }


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    models = fetch_models()
    records = []

    for i, m in enumerate(models, 1):
        name = m.get("name", "").strip()
        rec = schema.new_record()
        rec.update({
            "brand": "比亚迪",
            "model": name,
            "category": _category(name),
            "price_text": m.get("price", ""),
            "highlights": "；".join(m.get("features") or []),
            "url": f"https://www.byd.com{m.get('detailPage', {}).get('_path', '')}",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        start, end = _parse_price(m.get("price", ""))
        rec["price_min"], rec["price_max"] = start, end

        try:
            data = fetch_params(m["id"], m.get("salesNetworkId", 2))
        except Exception as e:
            print(f"  ! spec fetch failed for {name}: {e}")
            data = {}
        if data.get("configs"):
            rec.update(_parse_specs(data, name))
        else:
            rec["powertrain"] = "纯电" if "EV" in name else ("插混" if "DM" in name else "")

        records.append(rec)
        print(f"  [{i}/{len(models)}] {name}")
        time.sleep(REQUEST_DELAY)

    return records


if __name__ == "__main__":
    from ..core.writer import write_brand
    write_brand("byd", fetch())
