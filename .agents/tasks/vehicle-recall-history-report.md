# 车型历史召回信息数据流重构与审计报告

## 当前数据流与写入完整性

召回信息现在是标准车型数据的一部分，而非 `site/` 或 writer 的旁路补丁。唯一生产路径是：`scrapers/recalls/scraper.py.fetch()` 读取并严格验证版本控制的 catalog/候选快照，返回完整 18 字段的 projection records；`scrapers/run_all.py` 的首个 `REGISTRY` entry（`recalls` enrichment）在内存中将 projection 融合到八个品牌 adapter 的 `fetch()` records，随后才调用通用 `write_brands()` 写入 `data/<brand>.json` 与 `.csv`。`site/build_site.py` 仅验证通用车型 schema 并全量合并 `data/*.json`。

`writer.py` 不导入召回包、不读取 catalog/snapshot、也不注入字段。`write_brands(records_by_key, expected_keys=...)` 在 staging 前要求授权 key 集精确相等，因此 enrichment 不可能写出 `data/recalls.json`，也不能漏写或额外写品牌。`run_all` 的八品牌批次在 `.scraper-transactions/.lock` 保护下写入，并在任何目标 `os.replace` 前完成以下持久协议：为全部 16 个目标在同一 journal 文件系统内写入完整 `old/` 与 `new/` staging 字节、逐个 flush/fsync、fsync journal 目录；以临时文件 fsync 后原子替换 `manifest.json`，并 fsync journal 与其父目录。manifest 记录 transaction ID、相对目标路径、old/new backup 名称与 SHA-256、目标存在性、阶段及替换进度，阶段严格为 `prepared → committing → committed`。

`prepared` 持久化完成前禁止替换目标；每个替换后的目标临时文件、目标目录和 hash 都被同步确认，并持久写入 `committing` 进度。任何可捕获的 `prepared/committing` 失败会在持锁状态下恢复**全部**旧字节并逐项校验；不可捕获终止则由下一次持锁的 `run_all`、单品牌 adapter、writer、site build 或 validator 入口恢复。恢复遇到 `prepared`/`committing` 一律回滚到完整旧批次；遇到 `committed` 则严格确认全部新 hash 后前滚保留完整新批次。`committed` journal 会先原子重命名为 cleanup 标记并 fsync 父目录，再删除；崩溃在清理中时下一入口仅完成该安全删除。因此不会无声保留 JSON/CSV 或跨品牌的混合批次；缺失 manifest、备份或 hash 不符会显式以 `disk state unknown` 类错误中止，而非猜测。单品牌模块入口改为 `run_all.refresh_brand(key)`；它同样先加载全局审计并只校验该品牌范围的显式 target，避免单品牌刷新覆盖已审计字段。

所有召回运行逻辑、catalog、候选快照、adapter 和 validator 位于 `scrapers/recalls/`；根目录没有运行时 `recalls/` 目录。`data/` 仅保留八个品牌的 JSON/CSV。审计路径由 `PACKAGE_DIR`、`REPO_ROOT` 分别派生；catalog 仅可引用 `scrapers/recalls/snapshots/<id>.json`，绝对路径、遍历路径和 package 外路径被拒绝。

## 正确性口径、官方来源与覆盖边界

当前审计截止日为 **2026-10-01**。候选快照为 [`scrapers/recalls/snapshots/samr-su7-2026-10-01.json`](../../scrapers/recalls/snapshots/samr-su7-2026-10-01.json)，规范 SHA-256 为 `795622610431312c258dc062c67b7350be328b84002718baf28f21281df17a49`。它记录当前车型库中小米 SU7 车系的两条市场监管总局候选；实际检索的公告发布日期范围为 **2025-01-24 至 2025-09-19**。

展示事实只来自 allowlist 的一手 HTTPS 来源：市场监管总局召回栏目 <https://www.samr.gov.cn/zw/zh/index.html>、[2025-01-24 官方公告](https://www.samr.gov.cn/zw/zh/art/2025/art_589f94caf6ad48a484fcf97135ca3eb5.html)、[2025-09-19 官方公告](https://www.samr.gov.cn/zw/zh/art/2025/art_2152a981c72a430b9d10d6ef5cd9c09b.html)，以及仅作车型身份辅助证据的小米官方页 <https://www.xiaomiev.com/su7>。媒体、搜索摘要和第三方数据库不进入展示事实。

catalog 固定拒绝 `snapshot_exceptions`、`exceptions` 与任何未知顶层键。每个 snapshot candidate 有唯一 investigation 和终态；included candidate 需要同源同 URL 的 notice、event、match/skip 闭合，且 catalog 的 investigation/notice/event 集合必须恰好等于快照链路。每项投影 `DISPLAY_KEYS` 字符串均限制为 0..2048 字符，以匹配前端；非展示 evidence/excerpt 可为 0..4000 字符。

这只是截至上述日期、针对 SU7 的可审计定向候选集，**不是全国、任一品牌、任一栏目或任意时间区间的完整召回史**。“暂无已匹配记录”只表示当前快照范围内没有可靠映射，**不代表从未召回**。车系记录按聚合车型展示；车辆是否在召回范围仍需以官方 VIN 查询和公告中的生产期、版本、车辆型号为准。

## 审计链路、匹配与保守跳过

[candidate:samr-su7-2025-01-24] 对应截至 2026-10-01 快照中的 `included_matched` 候选。
[investigation:inv-samr-su7-2025-01] 已核验市场监管总局正文，终态 `verified_notice`。
[notice:samr-su7-2025-01-24] 公告日/实施日为 2025-01-24，范围为 2024-02-06 至 2024-11-26 生产的部分 SU7 标准版（30,931 台）；原文说明授时同步异常可能影响智能泊车静态障碍物探测，措施为免费 OTA 升级。
[event:su7-parking-2025-01] 独立的智能泊车静态障碍物探测事件。
[match:match-su7-parking-2025-01] 仅保守匹配 `(小米, SU7)`；品牌、车系、生产期、历史代际和纯电/标准版限制均有官方证据。
[skip:skip-su7-ultra-parking-2025-01] `(小米, SU7 Ultra)` 保持 `variant_unconfirmed`：公告只证明 SU7 标准版，不能扩大到 Ultra。

[candidate:samr-su7-2025-09-19] 对应第二条 `included_matched` 候选。
[investigation:inv-samr-su7-2025-09] 已核验市场监管总局正文，终态 `verified_notice`。
[notice:samr-su7-2025-09-19] 公告日/实施日为 2025-09-19，编号为 `S2025M0149I、S2025M0150I`，范围为 2024-02-06 至 2025-08-30 生产的部分 SU7 标准版（116,887 辆）；原文说明 L2 高速领航极端场景识别、预警或处置可能不足，措施为免费 OTA 升级。
[event:su7-l2-highway-2025-09] 独立的 L2 高速领航极端场景处置事件。
[match:match-su7-l2-2025-09] 同样仅映射 `(小米, SU7)`，并保留公告限制。
[skip:skip-su7-ultra-l2-2025-09] `(小米, SU7 Ultra)` 继续为 `variant_unconfirmed`，其 `recall_history` 严格为 `""`。

两公告经过 `duplicate_reviews` 判为 `distinct_event`，因为公告日期、车辆数和缺陷功能不同。除 SU7 外，当前 89 个车型没有可靠 match，字段严格为 `""`。

## 维护与验证

维护顺序：先在 `scrapers/recalls/snapshots/` 版本化官方候选及摘要，再更新 `scrapers/recalls/catalog.json` 的逐字段证据、事件去重、match/skip；运行：

```bash
.venv/bin/python -m scrapers.recalls.scraper
.venv/bin/python -m scrapers.recalls.validate
.venv/bin/python -m scrapers.recalls.validate --self-test
.venv/bin/python scrapers/run_all.py
.venv/bin/python site/build_site.py
```

本次持久事务修复实际执行并通过：迁移模块 `py_compile`；recall adapter 输出两个 projection target；严格负例自测输出 37 个拒绝场景；新增事务自测以真实八品牌 fixture 的 91 条 records、16 个 JSON/CSV 目标运行 22 个恢复场景。它用绕过 `finally` 的 `BaseException` 模拟进程中断，覆盖首个 staging backup、prepared 前后、每一个第 1..16 次 `os.replace` 后、commit 标记前和清理前；每个中断均由新的持锁 session 恢复并断言 16 文件严格等于完整旧批次或完整新批次、91 条/18 字段、CSV UTF-8 BOM/列序不变，且 `.scraper-transactions/` 只剩 `.lock`，没有 journal 或 backup 泄漏。离线 registry 集成将当前八份品牌 JSON 作为 fixture，确认 9 个 registry 项、91 条 records、并以事务批量提交后逐字节不改变现有 16 个品牌产物；`site/build_site.py` 重建 91 条 docs records；默认 validator 确认 2 investigations、2 notices、2 events、2 matches、2 skips、91 辆车型及 JSON/CSV/docs 投影一致。完整真实 `run_all` 未在本报告中声称执行，因为八个官方上游的网络请求耗时和可用性不适合作为本地重构验证前提。

HTTP 浏览器实际验证：`http.server` 下页面加载 91 行且 Console 无 error/warn；`S2025M0150I` 搜索仅命中 SU7，点击“查看详情”显示两条 SAMR 官方记录，包含范围、原因、OTA 措施和编号；小米/纯电/轿车三个筛选与关键词组合可用，清空后确认 SU7 Ultra 显示“暂无已匹配记录”；价格、续航、电量表头均点击排序。页面详情链接为精确 SAMR HTTPS URL，并实际读取到 `target="_blank"` 和 `rel="noopener noreferrer"`。验证服务已停止。最终 `git diff --check` 成功；根目录 `recalls/`、`data/recalls.json`、`data/recalls.csv` 均不存在。剩余低风险限制仅为上文明确的审计覆盖范围，而不是已展示两项 SAMR 事实的来源或映射正确性限制。
