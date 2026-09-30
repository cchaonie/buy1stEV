# AGENTS.md

面向在此仓库工作的编码智能体的操作指南。项目概述、数据字段说明与免责声明见 [README.md](README.md)，本文件只写「怎么改、改哪里、有什么坑」。

## 环境与命令

```bash
# 解释器：仓库内已有可用虚拟环境，优先直接用，不要重新 pip install
.venv/bin/python --version          # 3.14.x（CI 用 3.12）
.venv/bin/python -c "import requests; print(requests.__version__)"

# 抓取全部品牌（串行，数分钟，会重写 data/*.json 与 data/*.csv）
.venv/bin/python scrapers/run_all.py

# 只抓一个品牌（推荐：改动单个适配器时用）
.venv/bin/python -m scrapers.xiaomi.scraper

# 合并数据 -> docs/data.json（约 1 秒，幂等）
.venv/bin/python site/build_site.py

# 本地预览查看器（必须走 HTTP，file:// 下 fetch 会被拦）
.venv/bin/python -m http.server 8000 --directory docs
```

- 系统 `python3` **没有** `requests`，不要用它跑抓取。
- 仓库没有测试框架、没有 lint 配置、没有 `requirements.txt`/`pyproject.toml`（CI 中直接 `pip install requests`）。
- 修改适配器后最低验证标准：`.venv/bin/python -m scrapers.<key>.scraper` 能跑通，且产出记录字段齐全、价格/续航量级合理。

## 架构约束

数据流水线是单向的，改动时必须保持这个方向：

```
scrapers/<brand>/scraper.py  --write_brand()-->  data/<brand>.{json,csv}
                                                        |
                                        site/build_site.py（glob data/*.json 全量合并）
                                                        v
                                        docs/data.json  -->  docs/index.html（fetch 渲染）
```

- 每个适配器只暴露 `fetch() -> list[dict]`，**只返回数据，不自己写文件**；写盘统一由 [`scrapers/core/writer.py`](scrapers/core/writer.py#L12) 的 `write_brand(key, records)` 负责。
- 新品牌必须在 [`scrapers/run_all.py`](scrapers/run_all.py#L11) 的 `REGISTRY` 里登记，否则定时任务不会跑它。
- 字段定义是唯一真源：[`scrapers/core/schema.py`](scrapers/core/schema.py#L3) 的 `FIELDS`。改字段要同步四处——`FIELDS`、`LABELS`、[`docs/index.html`](docs/index.html#L57) 的表头与单元格取值逻辑、README 的字段表。
- 抓取用 `call_with_retry`（[`scrapers/core/retry.py`](scrapers/core/retry.py#L28)），并提供 `classify(resp)` 回调把响应判为 `"ok"` / `"retry"` / `"fatal"`；不要自己写 `time.sleep` 重试循环。
- `render()` 每次会清空 `tbody` 后重建全部行（搜索、筛选、排序都会触发），因此**展开状态不会保留**：用户点开「查看更多」后再筛选或排序，该行会收回 3 行。这是当前设计，改动前先想清楚是否要做状态保留。

## 数据规范

- 用 `schema.new_record()` 起手，它会把 17 个字段全部初始化为 `""`。
- **未知值写 `""`，不写 `None`/`null`，也不要删 key**。前端 `index.html` 依赖字段恒存在。
- `brand` 用中文（`"小鹏"`、`"问界"`…），因为 `index.html` 的 `LOGOS` 映射以中文名为键；`model` 用官方写法（`Model 3`、`SU7 Ultra`）。
- `price_min`/`price_max` 是**元**的整数；`price_text` 保留官网原文（如 `"21.99万 - 30.39万"`）。三者并存，别只填其一。
- `fetched_at` 在每个 `fetch()` 入口取一次 UTC 时间戳（`datetime.now(timezone.utc).isoformat()`），全批次共用同一个值。
- 多配置车型聚合成一条记录：价格取区间，续航/电量取最大值——这是既有约定，见 README「已知口径与局限」。

## 抓取适配器注意事项

上游是各品牌官网与私有接口，**随时可能变**。改动前先读该 `scraper.py` 顶部的 docstring，反爬姿势都记在那里：

- `byd`：两套 API，商品参数接口需要 HMAC-SHA256 签名（`X-HMAC-*` 头，时间戳）；限流码 `30001` 要判成可重试。
- `nio`：站点有腾讯 EdgeOne WAF，普通 UA 返回 HTTP 567 拦截页，适配器靠 **Googlebot UA** 拿官方 HTML（含 JSON-LD）。改 UA 会直接失效。
- `zeekr`：网关要求 H5 签名（`x_ca_key`/`x_ca_nonce`/`x_ca_timestamp` 毫秒 + `x_ca_sign` = SHA1(排序拼接)），secret 嵌在官方前端包里；注意用的是 `api-gw-toc`，不是 `api-gw-external`。
- `tesla`：从 `/model3/design` 等页面里抠 `const dataJson = {...}`，取 `group == "TRIM"` 的项。
- `xpeng` / `aito` / `xiaomi` / `nio` 有**手维护的 `MODELS` 车型清单**（[`xiaomi`](scrapers/xiaomi/scraper.py#L26)、[`tesla`](scrapers/tesla/scraper.py#L33)、[`nio`](scrapers/nio/scraper.py#L33)、[`xpeng`](scrapers/xpeng/scraper.py#L33)、[`aito`](scrapers/aito/scraper.py#L37)）：新车型上市必须手动补一行，否则抓不到。
- `lixiang` / `byd` / `zeekr` 从接口拿列表，无需手维护清单。
- 单个车型抓失败时，适配器惯例是记 `data = {}` 并打印 `! ... failed`，**继续跑完其余车型**；不要让一个车型的异常中断整批。

## 常见坑

- **别把非品牌 JSON 放进 `data/`**：[`site/build_site.py`](site/build_site.py#L13) 用 `glob("*.json")` 全量合并，任何多余文件都会被当车型数据并进 `docs/data.json`。
- `run_all.py` **没有逐品牌异常隔离**：某个适配器抛异常会中断整个批次，导致后面的品牌本次不更新。跨品牌改动时注意这一点。
- `data/*.csv` 是 `utf-8-sig` 编码，手工查看/编辑时留意 BOM。
- `docs/data.json` 是**生成物**，不要手改；改数据请改适配器或 `data/*.json` 后重新构建。
- 抓取会真实发起网络请求。不要为了「看看输出」反复跑 `run_all.py`；优先跑单个品牌，或只跑 `site/build_site.py`。
- 抓取结果会**覆盖** `data/` 下对应文件。提交前用 `git diff` 确认变化符合预期（正常应只有价格/参数/`fetched_at` 的变化）。

## 验证清单

改完适配器后：

```bash
.venv/bin/python -m scrapers.<key>.scraper     # 1. 抓取成功，记录数与官网在售车型数相当
.venv/bin/python site/build_site.py           # 2. 合并成功
git diff --stat data/ docs/                   # 3. 只有预期文件/字段变化
.venv/bin/python -m http.server 8000 --directory docs   # 4. 页面能加载、筛选与排序正常
```

改动 `index.html` 后务必在浏览器里实际打开验证（Console 无报错、表格有数据、四个筛选控件可用）；不要在未加载页面的情况下宣称前端改动已完成。

## 提交与 CI

- 数据刷新由 `scrape.yml` 每日 01:30 UTC 自动提交，信息为 `chore: refresh vehicle data [skip ci]`。**不要手工制造这类提交**，除非用户明确要求刷新数据。
- 含 `[skip ci]` 的提交不触发 CI，这是防止抓取循环的有意设计。
- 触碰 `data/`、`docs/`、`site/` 的 push 会触发 `deploy-pages.yml` 重新发布 Pages。
- 只有抓取逻辑或前端页面变化才需要人写提交信息；纯数据 diff 交给 CI。
