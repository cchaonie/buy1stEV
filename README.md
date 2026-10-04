# buy1stEV · 买第一辆电车

> 从 8 家中国新能源品牌官方接口/官网抓取在售车型价格与参数，归一化为统一 schema，并生成一个可排序、可筛选的静态车型库页面。

**Buy your first EV — a unified dataset of on-sale Chinese EV models, scraped nightly from official brand sources.**

数据每周一自动更新：所有数字均来自品牌官方源（官网 / 官方 API），不经过第三方聚合站。

---

## 项目在做什么

1. **抓取**：每个品牌一个独立适配器（`scrapers/<brand>/scraper.py`），负责请求官方源、解析出该品牌特有的结构。
2. **归一**：所有适配器输出同一套字段（`scrapers/core/schema.py`），写入 `data/<brand>.json` 与 `data/<brand>.csv`。
3. **合并**：`site/build_site.py` 把所有 `data/*.json` 合并为 `docs/data.json`。
4. **展示**：`docs/index.html` 是 GitHub Pages 的购车指南入口页；`docs/models.html` 是零依赖的原生 JS 数据与召回查看器，读取 `docs/data.json` 后渲染成可搜索、可筛选、可排序的表格。过长的「定位亮点」默认截断为 3 行，可点「查看更多」展开。
5. **召回审计**：`scrapers/recalls/` 是受 `scrapers/run_all.py` 正式调度的 enrichment adapter；它从版本控制的官方候选快照和 catalog 生成 `recall_history` projection，并在统一 `write_brand()` 前合并到品牌 records。writer 与站点构建器不读取、注入或补写召回数据；`scrapers.recalls.validate` 离线阻断 catalog、生成数据与 `docs/models.html` 查看器之间的不一致。

整个流程由 GitHub Actions 每天定时执行，抓取结果自动提交回仓库。

## 覆盖品牌

| 品牌   | key       | 官方源        | 抓取方式                                       | 手维护车型清单     |
| ------ | --------- | ------------- | ---------------------------------------------- | ------------------ |
| 比亚迪 | `byd`     | byd.com       | 官方 CMS/商品 API（HMAC-SHA256 签名）          | 否（接口返回列表） |
| 小米   | `xiaomi`  | xiaomiev.com  | 官方版本列表 POST 接口（无鉴权）               | 是                 |
| 特斯拉 | `tesla`   | tesla.cn      | 配置器页面内嵌 `dataJson` JS 对象              | 是                 |
| 蔚来   | `nio`     | nio.cn        | 官网 HTML（JSON-LD，需 Googlebot UA 绕过 WAF） | 是                 |
| 小鹏   | `xpeng`   | xiaopeng.com  | 配置页 React Server Component flight payload   | 是                 |
| 理想   | `lixiang` | lixiang.com   | 官方 `all-on-sale` 开放 GET 接口               | 否（接口返回列表） |
| 极氪   | `zeekr`   | zeekrlife.com | 官方 MSE 网关（H5 签名 `x_ca_sign`）           | 否（接口返回列表） |
| 问界   | `aito`    | aito.auto     | 官网静态参数表 HTML                            | 是                 |

> 其中部分站点有反爬（WAF / 签名 / 需要特定 UA 与 Referer），适配器已内置对应处理；细节见各 `scraper.py` 顶部 docstring。

## 目录结构

```
buy1stEV/
├── scrapers/
│   ├── core/
│   │   ├── schema.py      # 规范字段 FIELDS、中文表头 LABELS、价格分档
│   │   ├── retry.py       # 指数退避重试 + 抖动 + 可重试/致命错误分类
│   │   └── writer.py      # 写出 data/<brand>.json 与 .csv
│   ├── recalls/           # 官方候选快照、catalog、projection adapter 与离线校验
│   │   ├── catalog.json   # 覆盖边界、逐字段证据、事件与匹配/跳过审计
│   │   ├── snapshots/     # 版本控制的官方候选公告快照（不参与 data/ 合并）
│   │   ├── scraper.py    # fetch() -> canonical recall projection records（不写盘）
│   │   └── validate.py   # 全链路校验与负例自测
│   ├── run_all.py         # brands + recalls registry，内存融合后批量写入
│   └── <brand>/scraper.py # 各品牌适配器，统一暴露 fetch()
├── data/                  # 抓取产物（每品牌 json + csv，提交进仓库）
├── site/build_site.py     # 合并 data/*.json -> docs/data.json
├── docs/                  # GitHub Pages 根目录
│   ├── index.html         # 购车指南入口页
│   ├── models.html        # 数据与召回查看器（原生 JS，fetch data.json）
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

数据与召回查看器 `docs/models.html` 使用 `fetch("data.json")`，受同源策略限制，**必须通过 HTTP 服务打开**，直接双击 `file://` 会加载失败：

```bash
.venv/bin/python -m http.server 8000 --directory docs
# 购车指南入口：http://localhost:8000/
# 数据与召回查看器：http://localhost:8000/models.html
```

## 数据字段

`scrapers/core/schema.py` 定义 18 个规范字段，抓不到的字段一律写空字符串 `""`（不写 `null`，也不省略 key）：

| 字段                      | 含义                                   | 字段                | 含义                       |
| ------------------------- | -------------------------------------- | ------------------- | -------------------------- |
| `brand`                   | 品牌（中文，如「比亚迪」）             | `battery_kwh`       | 电池容量 kWh               |
| `model`                   | 车型名                                 | `ac_charge_kw`      | 交流充电功率 kW            |
| `powertrain`              | 动力类型（纯电/插混/增程…）            | `charging_platform` | 充电平台（如 897V）        |
| `category`                | 类别（轿车/SUV/MPV/轿跑）              | `adas`              | 智驾方案                   |
| `price_min` / `price_max` | 价格区间（单位：元，整数）             | `highlights`        | 定位亮点，多段以「；」拼接 |
| `price_text`              | 官方原始价格文案                       | `url`               | 官方车型页                 |
| `range_cltc_km`           | CLTC 续航 km                           | `source`            | 来源域名                   |
| `battery_type`            | 电池类型                               | `fetched_at`        | 采集时间（UTC ISO 8601）   |
| `recall_history`          | 召回信息；非空时为紧凑 JSON 数组字符串 |                     |                            |

`recall_history` 为兼容 JSON/CSV 的标量字符串。每个数组项包含内部事件/公告 ID、发布机构、官方 URL、公告/实施日期、官方召回编号（未提供时为空）、适用范围、原因、措施和核验日期；多事件按日期稳定排序。无可靠匹配时严格为 `""`。

JSON 为 UTF-8 无转义（`ensure_ascii=False`）；CSV 为 `utf-8-sig` 编码，便于 Excel 直接打开。

### 召回资料的来源、覆盖与维护

当前召回清单的审计截止日为 **2026-10-01**。本轮候选输入固定在版本控制的 [`scrapers/recalls/snapshots/samr-su7-2026-10-01.json`](scrapers/recalls/snapshots/samr-su7-2026-10-01.json)：该文件记录国家市场监督管理总局官方入口、检索/覆盖截止日、两条候选的稳定 ID、官方 URL、公告日、官方范围、调查决定和规范 SHA-256。`scrapers/recalls/catalog.json` 的来源行引用该快照及摘要；受调度的 recall adapter 在内存中生成 projection，`run_all` 在调用统一 writer 前将其融合进相应品牌 records。

该快照是截至 **2026-10-01**、针对当前车型库 **小米 SU7** 车系的可审计调查候选集；国家市场监督管理总局来源在本次快照中实际检索的公告发布日期区间为 **2025-01-24 至 2025-09-19**：

- 国家市场监督管理总局召回栏目：<https://www.samr.gov.cn/zw/zh/index.html>
- 2025-01-24 SU7 标准版公告：<https://www.samr.gov.cn/zw/zh/art/2025/art_589f94caf6ad48a484fcf97135ca3eb5.html>
- 2025-09-19 SU7 标准版公告：<https://www.samr.gov.cn/zw/zh/art/2025/art_2152a981c72a430b9d10d6ef5cd9c09b.html>

事实仅采用市场监管总局、缺陷产品召回技术中心或厂商官方一手公告；搜索结果和媒体只能帮助发现候选。车型匹配要求公告与当前 `(brand, model)` 的车系、生产期、代际、动力/版本及进口/国产边界可保守对齐，歧义项跳过。页面“暂无已匹配记录”只表示在上述实际覆盖内暂无可靠映射，**不代表从未召回**。该快照覆盖的是本次车型库和截止日的可审计调查候选集，**不是全国官方召回史全量、也不是对该栏目、品牌或日期区间的穷举**；官方索引稳定性和旧页面留存也有缺口，**不保证历史绝对完整**。

维护者先更新 `scrapers/recalls/snapshots/` 中的候选快照（并更新 catalog 来源行的路径/摘要），再逐篇打开官方正文，更新 `scrapers/recalls/catalog.json` 的逐字段证据、事件去重和 match/skip；随后执行：

```bash
.venv/bin/python -m scrapers.recalls.scraper
.venv/bin/python -m scrapers.recalls.validate
.venv/bin/python -m scrapers.recalls.validate --self-test
.venv/bin/python site/build_site.py
```

每日 `run_all` 的 `recalls` registry entry 会先验证审计输入，再和每个品牌的 canonical records 融合，最后以单批事务写入八个品牌文件；单品牌命令同样先消费其范围内的 projection，不会清空小米的已审计历史。召回文字参与关键词搜索（机构、标题、日期、编号、范围、原因和措施）；折叠状态不影响搜索。

### 已知口径与局限（查看数据时请注意）

- **一车型一条记录**：同一车型的多个配置被聚合成一条，价格取区间、续航/电量取该车型的**最大值**。因此「续航」列不代表单一配置值。
- **部分续航为车系上限**：如蔚来 ET5 (1055km) 对应 150kWh 电池版本。
- **增程车的续航口径不统一**：问界记录的是「综合续航」，其余品牌多为「纯电续航」，跨品牌比较续航时需留意。
- **`powertrain` 取值尚未完全收敛**：目前存在 `增程/纯电` 与 `增程 / 纯电`（空格差异）两种写法，来自不同适配器。
- **`highlights` 在页面上有截断**：表格中默认只显示 3 行，超出部分需点「查看更多」展开；搜索匹配的是完整原文，不受截断影响。
- **字段覆盖率不均**：`ac_charge_kw`（69/91 为空）与 `charging_platform`（60/91 为空）缺失较多，因为多数官方接口不暴露该数据。
- 数据由脚本自动抓取，**仅供参考**，购车决策请以品牌官方最新发布为准。

## 数据新鲜度

| 工作流             | 触发                                            | 作用                                                             |
| ------------------ | ----------------------------------------------- | ---------------------------------------------------------------- |
| `scrape.yml`       | 每周一北京时间 01:00 + 手动                     | 抓取 → 构建 → 提交 `data/*`、`docs/*`（`[skip ci]`）→ 发布 Pages |
| `deploy-pages.yml` | push 到 `main` 且涉及 `data/`、`docs/`、`site/` | 重新构建并发布 Pages                                             |

提交信息固定为 `chore: refresh vehicle data [skip ci]`，因此 `main` 的历史即为数据快照时间线。

## 扩展一个新品牌

1. 新建 `scrapers/<key>/scraper.py`，暴露 `fetch() -> list[dict]`，用 `schema.new_record()` 初始化、`schema` 里的常量做映射，网络请求统一走 `call_with_retry`。
2. 在 `scrapers/run_all.py` 的 `REGISTRY` 中登记 `(key, "scrapers.<key>.scraper", "fetch")`。
3. 把车标放进 `docs/logos/`，并在 `docs/models.html` 的 `LOGOS` 映射里加上「中文品牌名 → 车标路径」。

## 免责声明

本项目仅用于技术学习与信息聚合，数据版权归各品牌所有，不提供任何购车建议或商业服务。请遵守目标站点的 robots.txt 与服务条款，勿高频请求。
