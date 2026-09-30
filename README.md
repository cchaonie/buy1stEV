# buy1stEV · 买第一辆电车

> 从 8 家中国新能源品牌官方接口/官网抓取在售车型价格与参数，归一化为统一 schema，并生成一个可排序、可筛选的静态车型库页面。

**Buy your first EV — a unified dataset of on-sale Chinese EV models, scraped nightly from official brand sources.**

数据每日自动更新：所有数字均来自品牌官方源（官网 / 官方 API），不经过第三方聚合站。

---

## 项目在做什么

1. **抓取**：每个品牌一个独立适配器（`scrapers/<brand>/scraper.py`），负责请求官方源、解析出该品牌特有的结构。
2. **归一**：所有适配器输出同一套字段（`scrapers/core/schema.py`），写入 `data/<brand>.json` 与 `data/<brand>.csv`。
3. **合并**：`site/build_site.py` 把所有 `data/*.json` 合并为 `docs/data.json`。
4. **展示**：`docs/index.html` 是一个零依赖的纯前端页面（原生 JS），读取 `docs/data.json` 后渲染成可搜索、可筛选、可排序的表格，通过 GitHub Pages 发布。过长的「定位亮点」默认截断为 3 行，可点「查看更多」展开。

整个流程由 GitHub Actions 每天定时执行，抓取结果自动提交回仓库。

## 覆盖品牌

| 品牌 | key | 官方源 | 抓取方式 | 手维护车型清单 |
| --- | --- | --- | --- | --- |
| 比亚迪 | `byd` | byd.com | 官方 CMS/商品 API（HMAC-SHA256 签名） | 否（接口返回列表） |
| 小米 | `xiaomi` | xiaomiev.com | 官方版本列表 POST 接口（无鉴权） | 是 |
| 特斯拉 | `tesla` | tesla.cn | 配置器页面内嵌 `dataJson` JS 对象 | 是 |
| 蔚来 | `nio` | nio.cn | 官网 HTML（JSON-LD，需 Googlebot UA 绕过 WAF） | 是 |
| 小鹏 | `xpeng` | xiaopeng.com | 配置页 React Server Component flight payload | 是 |
| 理想 | `lixiang` | lixiang.com | 官方 `all-on-sale` 开放 GET 接口 | 否（接口返回列表） |
| 极氪 | `zeekr` | zeekrlife.com | 官方 MSE 网关（H5 签名 `x_ca_sign`） | 否（接口返回列表） |
| 问界 | `aito` | aito.auto | 官网静态参数表 HTML | 是 |

> 其中部分站点有反爬（WAF / 签名 / 需要特定 UA 与 Referer），适配器已内置对应处理；细节见各 `scraper.py` 顶部 docstring。

## 目录结构

```
buy1stEV/
├── scrapers/
│   ├── core/
│   │   ├── schema.py      # 规范字段 FIELDS、中文表头 LABELS、价格分档
│   │   ├── retry.py       # 指数退避重试 + 抖动 + 可重试/致命错误分类
│   │   └── writer.py      # 写出 data/<brand>.json 与 .csv
│   ├── run_all.py         # 品牌注册表 REGISTRY，串行跑完全部适配器
│   └── <brand>/scraper.py # 各品牌适配器，统一暴露 fetch()
├── data/                  # 抓取产物（每品牌 json + csv，提交进仓库）
├── site/build_site.py     # 合并 data/*.json -> docs/data.json
├── docs/                  # GitHub Pages 根目录
│   ├── index.html         # 静态查看器（原生 JS，无构建步骤）
│   ├── data.json          # 合并后的全量数据
│   └── logos/             # 品牌车标
└── .github/workflows/
    ├── scrape.yml         # 每日 01:30 UTC 抓取 + 提交 + 发布 Pages
    └── deploy-pages.yml   # push 到 main 时重新构建并发布
```

## 快速开始

要求 Python 3.12+（CI 使用 3.12，本地开发环境为 3.14），唯一第三方依赖是 `requests`。

```bash
git clone git@github.com:cchaonie/buy1stEV.git
cd buy1stEV

python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

仓库内已有一个本地虚拟环境 `.venv/`（不入库），可直接使用 `.venv/bin/python` 而无需激活。

### 抓取全部品牌

```bash
.venv/bin/python scrapers/run_all.py
```

输出示例：

```
OK: byd -> 46 records -> .../data/byd.json
OK: xiaomi -> 5 records -> .../data/xiaomi.json
...
```

抓取是**串行**的，并带有请求间隔（BYD 适配器固定 1s），全部跑完需要数分钟。

### 增量抓取单个品牌

每个适配器都可以直接运行，只刷新自己的数据文件：

```bash
.venv/bin/python -m scrapers.xiaomi.scraper
.venv/bin/python -m scrapers.nio.scraper
```

### 构建站点数据

```bash
.venv/bin/python site/build_site.py
# OK: 91 records -> docs/data.json
```

### 本地预览页面

查看器使用 `fetch("data.json")`，受同源策略限制，**必须通过 HTTP 服务打开**，直接双击 `file://` 会加载失败：

```bash
.venv/bin/python -m http.server 8000 --directory docs
# 打开 http://localhost:8000/
```

## 数据字段

`scrapers/core/schema.py` 定义 17 个规范字段，抓不到的字段一律写空字符串 `""`（不写 `null`，也不省略 key）：

| 字段 | 含义 | 字段 | 含义 |
| --- | --- | --- | --- |
| `brand` | 品牌（中文，如「比亚迪」） | `battery_kwh` | 电池容量 kWh |
| `model` | 车型名 | `ac_charge_kw` | 交流充电功率 kW |
| `powertrain` | 动力类型（纯电/插混/增程…） | `charging_platform` | 充电平台（如 897V） |
| `category` | 类别（轿车/SUV/MPV/轿跑） | `adas` | 智驾方案 |
| `price_min` / `price_max` | 价格区间（单位：元，整数） | `highlights` | 定位亮点，多段以「；」拼接 |
| `price_text` | 官方原始价格文案 | `url` | 官方车型页 |
| `range_cltc_km` | CLTC 续航 km | `source` | 来源域名 |
| `battery_type` | 电池类型 | `fetched_at` | 采集时间（UTC ISO 8601） |

JSON 为 UTF-8 无转义（`ensure_ascii=False`）；CSV 为 `utf-8-sig` 编码，便于 Excel 直接打开。

### 已知口径与局限（查看数据时请注意）

- **一车型一条记录**：同一车型的多个配置被聚合成一条，价格取区间、续航/电量取该车型的**最大值**。因此「续航」列不代表单一配置值。
- **部分续航为车系上限**：如蔚来 ET5 (1055km) 对应 150kWh 电池版本。
- **增程车的续航口径不统一**：问界记录的是「综合续航」，其余品牌多为「纯电续航」，跨品牌比较续航时需留意。
- **`powertrain` 取值尚未完全收敛**：目前存在 `增程/纯电` 与 `增程 / 纯电`（空格差异）两种写法，来自不同适配器。
- **`highlights` 在页面上有截断**：表格中默认只显示 3 行，超出部分需点「查看更多」展开；搜索匹配的是完整原文，不受截断影响。
- **字段覆盖率不均**：`ac_charge_kw`（69/91 为空）与 `charging_platform`（60/91 为空）缺失较多，因为多数官方接口不暴露该数据。
- 数据由脚本自动抓取，**仅供参考**，购车决策请以品牌官方最新发布为准。

## 数据新鲜度

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `scrape.yml` | 每日 01:30 UTC + 手动 | 抓取 → 构建 → 提交 `data/*`、`docs/*`（`[skip ci]`）→ 发布 Pages |
| `deploy-pages.yml` | push 到 `main` 且涉及 `data/`、`docs/`、`site/` | 重新构建并发布 Pages |

提交信息固定为 `chore: refresh vehicle data [skip ci]`，因此 `main` 的历史即为数据快照时间线。

## 扩展一个新品牌

1. 新建 `scrapers/<key>/scraper.py`，暴露 `fetch() -> list[dict]`，用 `schema.new_record()` 初始化、`schema` 里的常量做映射，网络请求统一走 `call_with_retry`。
2. 在 `scrapers/run_all.py` 的 `REGISTRY` 中登记 `(key, "scrapers.<key>.scraper", "fetch")`。
3. 把车标放进 `docs/logos/`，并在 `docs/index.html` 的 `LOGOS` 映射里加上「中文品牌名 → 车标路径」。

## 免责声明

本项目仅用于技术学习与信息聚合，数据版权归各品牌所有，不提供任何购车建议或商业服务。请遵守目标站点的 robots.txt 与服务条款，勿高频请求。
