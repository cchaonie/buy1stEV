"""Canonical record schema shared by all brand scrapers."""

FIELDS = [
    "brand",
    "model",
    "powertrain",
    "category",
    "price_min",
    "price_max",
    "price_text",
    "range_cltc_km",
    "battery_type",
    "battery_kwh",
    "ac_charge_kw",
    "charging_platform",
    "adas",
    "highlights",
    "url",
    "source",
    "fetched_at",
    "recall_history",
]

LABELS = {
    "brand": "品牌",
    "model": "车型",
    "powertrain": "动力类型",
    "category": "类别",
    "price_min": "起售价",
    "price_max": "最高价",
    "price_text": "价格区间",
    "range_cltc_km": "续航(CLTC) km",
    "battery_type": "电池类型",
    "battery_kwh": "电池电量(kWh)",
    "ac_charge_kw": "交流充电功率(kW)",
    "charging_platform": "充电平台",
    "adas": "智驾",
    "highlights": "定位亮点",
    "url": "参考链接",
    "source": "来源",
    "fetched_at": "采集时间",
    "recall_history": "召回信息",
}

PRICE_BUCKETS = [
    ("<10万", None, 100_000),
    ("10-15万", 100_000, 150_000),
    ("15-20万", 150_000, 200_000),
    ("20-30万", 200_000, 300_000),
    ("30万+", 300_000, None),
]


def price_bucket(price_min):
    if price_min is None:
        return ""
    for label, lo, hi in PRICE_BUCKETS:
        if lo is not None and price_min < lo:
            continue
        if hi is not None and price_min >= hi:
            continue
        return label
    return ""


def new_record():
    return {f: "" for f in FIELDS}
