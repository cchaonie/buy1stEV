"""Zeekr (极氪) adapter: official China lineup, prices and specs.

Source: api-gw-toc.zeekrlife.com, the MSE gateway the official zeekrlife.com web
apps call. (api-gw-external.zeekrlife.com is only the AMap map-service proxy
declared in ``window._AMapSecurityConfig.serviceHost`` and rejects these model
routes with ``5020001 请求头时间无效``.)

The gateway authenticates every request with the web H5 signing scheme:
``x_ca_key`` + ``x_ca_nonce`` + ``x_ca_timestamp`` (epoch **milliseconds**) and
``x_ca_sign`` = SHA1(join(sorted([secret, nonce, timestamp]))). The shared
``H5-SIGN-SECRET-KEY`` secret below is embedded (AES-GCM obfuscated) in the
official web bundle and is validated by the gateway; a missing or mismatched
value returns HTTP 401.
"""

import hashlib
import random
import re
import time
from datetime import datetime, timezone

import requests

from ..core import schema
from ..core.retry import call_with_retry

API = "https://api-gw-toc.zeekrlife.com"
SOURCE = "zeekrlife.com"

SIGN_SECRET = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCz09z6e9WOcNq+nUMX8Vq1Xe2EmJxuR3XbturefioF)E(Fl"
SIGN_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz1234567890"
APP_ID = "ONEX97FB91F061405"

LIST_PATH = "/zeekrlife-config-order/v1/model/pub/toc/queryOnlineModelYear/list"
VERSION_PATH = "/zeekrlife-config-order/v1/model/pub/toc/modelVersion/list"

CATEGORY_MAP = {"SUV": "SUV", "MPV": "MPV", "轿车": "轿车", "猎装": "轿跑"}

RETRYABLE = {429, 500, 502, 503, 504}


def _headers():
    ts = str(int(time.time() * 1000))
    nonce = "".join(random.choice(SIGN_ALPHABET) for _ in range(15))
    sign = hashlib.sha1("".join(sorted([SIGN_SECRET, nonce, ts])).encode()).hexdigest()
    device = "".join(random.choice("0123456789") for _ in range(20))
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.zeekrlife.com",
        "Referer": "https://www.zeekrlife.com/",
        "app_type": "PC",
        "platform": "WEB_PC",
        "platform_h5": "",
        "risk_platform": "",
        "app_code": "toc_pc_zeekrlife",
        "WorkspaceId": "prod",
        "Version": "2",
        "device_id": device,
        "riskTimeStamp": ts,
        "AppId": APP_ID,
        "X-CORS-{}-prod".format(APP_ID): "1",
        "x_ca_key": "H5-SIGN-SECRET-KEY",
        "x_ca_nonce": nonce,
        "x_ca_timestamp": ts,
        "x_ca_sign": sign,
    }


def _classify(resp):
    if resp is None:
        return "retry"
    if resp.status_code == 200:
        return "ok"
    return "retry" if resp.status_code in RETRYABLE else "fatal"


def _post(path, payload):
    def go():
        return requests.post(API + path, headers=_headers(), json=payload, timeout=30)
    resp = call_with_retry(go, classify=_classify, retries=3, base_delay=1.0)
    data = resp.json()
    if data.get("code") != "000000":
        raise ValueError("api error {}: {}".format(data.get("code"), data.get("msg")))
    return data.get("data")


def _int(value):
    m = re.search(r"(\d+(?:\.\d+)?)", str(value or ""))
    return int(float(m.group(1))) if m else None


def _fmt(value):
    return "{:g}".format(value)


def _model_name(value):
    s = re.sub(r"^\d{4}(?:焕新)?款", "", str(value or ""))
    s = s.replace("焕新", "")
    s = re.sub(r"^极氪", "", s)
    return s.strip()


def _powertrain(row):
    hay = " ".join(
        [str(row.get("modelYearSlogan", ""))]
        + [str(sp.get("shard1", "")) for sp in row.get("sellPoint") or []]
    )
    if "增程" in hay:
        return "增程"
    if "混动" in hay or "电混" in hay:
        return "插混"
    return "纯电"


def _charging_platform(row):
    for sp in row.get("sellPoint") or []:
        if "高压" in str(sp.get("shard1", "")):
            val = "{}{}".format(sp.get("shard2", ""), sp.get("shard4", "")).strip()
            if val:
                return val
    return ""


def _model_range(row):
    for sp in row.get("sellPoint") or []:
        if "续航里程(CLTC)" in str(sp.get("shard1", "")):
            return _int(sp.get("shard2")) or ""
    return ""


def _highlights(row):
    parts = []
    slogan = str(row.get("modelYearSlogan", "")).strip()
    if slogan:
        parts.append(slogan)
    for sp in row.get("sellPoint") or []:
        text = "{}{}{}".format(sp.get("shard1", ""), sp.get("shard2", ""), sp.get("shard4", "")).strip()
        if text:
            parts.append(text)
    return "；".join(dict.fromkeys(parts))


def _parse_versions(versions):
    prices, ranges, kwhs, adas = [], [], [], []
    for v in versions:
        price = v.get("versionPrice")
        if price:
            prices.append(int(round(price)))
        for sp in v.get("sellPointResultList") or []:
            label = str(sp.get("shard1", ""))
            if "CLTC" in label and "综合续航" not in label:
                r = _int(sp.get("shard2"))
                if r:
                    ranges.append(r)
        name = str(v.get("versionName", ""))
        m = re.search(r"(\d+(?:\.\d+)?)\s*度", name)
        if m:
            kwhs.append(float(m.group(1)))
        for item in v.get("saleItemAndSkuResults") or []:
            for sku in item.get("skuResultList") or []:
                sku_name = str(sku.get("itemName", ""))
                names = [str(s.get("subSkuName", "")) for s in sku.get("subSkuResultList") or []]
                if "电池容量" in sku_name:
                    for n in names:
                        bm = re.search(r"(\d+(?:\.\d+)?)\s*kWh", n)
                        if bm:
                            kwhs.append(float(bm.group(1)))
                if "辅助驾驶" in sku_name:
                    adas.extend(n for n in names if n)
    result = {
        "range_cltc_km": max(ranges) if ranges else "",
        "battery_kwh": max(kwhs) if kwhs else "",
        "adas": " / ".join(dict.fromkeys(adas)),
    }
    if prices:
        result["price_min"] = min(prices)
        result["price_max"] = max(prices)
    return result


def fetch():
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows = _post(LIST_PATH, {}) or []
    records = []

    for row in rows:
        name = _model_name(row.get("modelYearAliasName") or row.get("homePageTabName"))
        rec = schema.new_record()
        rec.update({
            "brand": "极氪",
            "model": name,
            "powertrain": _powertrain(row),
            "category": CATEGORY_MAP.get(str(row.get("groupName", "")), ""),
            "range_cltc_km": _model_range(row),
            "charging_platform": _charging_platform(row),
            "highlights": _highlights(row),
            "url": row.get("learnMoreUrl") or row.get("paramConfigUrl") or "https://www.zeekrlife.com",
            "source": SOURCE,
            "fetched_at": fetched_at,
        })
        try:
            versions = _post(VERSION_PATH, {
                "modelInfoId": row.get("modelInfoId"),
                "modelYearCode": row.get("modelYearCode"),
                "couponChoose": None,
            }) or []
            rec.update({k: v for k, v in _parse_versions(versions).items() if v != ""})
        except Exception as e:
            print("  ! fetch failed for {}: {}".format(name, e))
        if rec["price_min"] != "":
            lo, hi = rec["price_min"] / 10000, rec["price_max"] / 10000
            rec["price_text"] = (
                "{}万 - {}万".format(_fmt(lo), _fmt(hi)) if lo != hi else "{}万".format(_fmt(lo))
            )
        records.append(rec)
        print("  [{}/{}] {} price={} range={} kwh={}".format(
            len(records), len(rows), name,
            rec["price_text"] or "-", rec["range_cltc_km"] or "-", rec["battery_kwh"] or "-",
        ))

    return records


if __name__ == "__main__":
    from ..core.writer import write_brand
    write_brand("zeekr", fetch())
