# fd-zhengxin-reference

> **Part of the [Lex line (寻数·识律)](https://www.finddatatech.cloud/products/lex) of [FindData](https://www.finddatatech.cloud)** — Zhengxin reference — probe samples for the zhengxin benchmark corpus.

政心（zhengxin 线，lex 第四模块）参照系 MCP：为「政心」政治思想理解工具提供外部
真源的**对接组装与查询**（不自建抽取）——政治概念（SEP 引用层 + 中国线词条）、
思潮坐标（MARPOR 立场编码 + 中国线）、思想关系边（Wikidata 对齐 + 中国线）、
政体指标时序（V-Dem + MARPOR）。形制照姊妹仓 fd-zhesi-reference（参照系服务仓先例）。

产品定位：政心做政治思想的结构化理解，参照系负责把「概念—思潮—人物—政体」锚定在
可溯源的外部坐标系上——每条结果携带 provenance（source / stable_id / as_of /
attribution / snapshot_version），同一查询在不同时间的结果差异可由快照版本解释；
查询未命中或源未就绪时如实返回空结果并带原因，绝不编造。

## 工具面（契约）

| 工具 | 能力 | 数据源 | 状态 |
|---|---|---|---|
| `concept_lookup` | 政治概念定义摘要 + 引用链接（正文不落库） | SEP + 中国线种子概念 + 近现代思潮索引 | 已接线（多源融合，逐行 source 标注） |
| `ideology_query` | 思潮/思想家/政党的政治坐标 | Wikidata（P1142/P1387）+ MARPOR | 已接线（MARPOR 门关 → not_ingested + license_unconfirmed） |
| `relation_query` | 思想关系边：类型/极性/权重 | Wikidata（极性过滤直通）+ 中国线种子边 | 已接线（多源融合） |
| `regime_timeseries` | 国家×年份政体指标序列 | V-Dem（含指标口径说明） | 已接线（摄取后直通；未摄取 not_ingested） |
| `entity_dossier` | **单实体读面**：人物/政党/事件/运动/学派 → 身份 + 学派 + 出入关系边全集 + 覆盖三态 | 先秦种子谱系 + Wikidata（CC0） | 已接线（种子 40 人物实测 40/40；wikidata 面当前生产未摄取 → `not_ingested`） |
| `source_status` | 六源摄取状态与缺口（未摄取必带原因） | 摄取台账表 + 许可门 | 已接线 |

启动初始化：`create_server` 以启动钩子形态幂等加载 curated 层（先秦种子谱系
`china-seed` + 近现代五思潮索引 `modern-index`，需 schema.init 后调用）；DB 为
`:memory:` 时跳过，seed 文件缺失时跳过并如实回报（缺口在 `source_status` 可见）。

## 源清单与许可门纪律

`source_status` 的六源 = 下表四外部源 + 两个 curated 层；表内「中国线」是
许可门（`license.py` LICENSE_REGISTRY）的语料层条目，不在六源台账之列。

| 源 | 用途 | 许可门 |
|---|---|---|
| V-Dem | 政体指标时序（v2x_* 等指数） | 已确认（CC BY-SA 4.0，非 CC BY；ShareAlike 只在导出面触发，摄取限定 Core/Country-Date 变体） |
| MARPOR | 政党宣言立场编码（rile 等） | 评审中（三点未确认：注册流程 / 科研用途边界 / 再分发授权；未过门不摄取） |
| Wikidata | 全量层对齐（QID 回填） | 已确认（CC0） |
| SEP | 概念引用层（只链不抄；政治思想词 26/26 命中实测） | 已确认（站点条款） |
| 中国线 | 政治思想语料（词条/人物/关系全文层） | 评审中（源清单待定；未过门不摄取） |

两个 curated 层（不走许可门，编辑产出的结构化数据，非外部语料摄取）：

| 层 | 内容 | 状态口径 |
|---|---|---|
| `china-seed` | 先秦诸子种子谱系（`seed/china_seed.json`；种子不造数值坐标） | 启动钩子幂等加载；结构契约自检 `scripts/check_seed.py`；seed 缺失时跳过并在 `source_status` 如实记 not_ingested |
| `modern-index` | 近现代五思潮文献索引（只存条目元数据与链接，不存正文） | 启动钩子以不可变快照加载，dataset 名 `china-modern-index` |

## 源许可台账指针

源评审与侦察证据统一落 `docs/`，许可判定以 `license.py` 的 LICENSE_REGISTRY
为准、`docs/data-license.md` 为证据台账（新源先补台账评审再动 registry）：

- `docs/data-license.md` —— **源许可台账主文档**：V-Dem / MARPOR 官网一手
  核验（2026-10-23），含许可原文引用、获取通道、对拟议用途的合规结论与
  未确认清单（MARPOR 授权联系 manifesto-communication@wzb.eu）。
- `docs/sep-coverage.md` —— SEP 政治思想词覆盖面实测（26/26 命中，含
  「只链不抄」边界在通道层的确认）。
- `docs/wikidata-probe.md` + `docs/wikidata-probe-samples/` —— Wikidata
  政治面 SPARQL 通道实测与 36 份样本（极性判据表与摄取器姿势的证据）。
- `docs/china-modern-sources.md` —— 中国近现代思潮公开学术源侦察与先秦
  种子谱系来源定案（modern-index 与 china-seed 的固化来源）。

许可门纪律（照 fd-zhesi-reference 的双门纪律）：**许可门**（静态事实：条款是否允许
入库）未通过 → 该源不摄取，`source_status` 与相关查询工具一律报 `not_ingested`
（reason=license_unconfirmed，带评审待办）；**可得性门**（动态事实：此刻能否取到）
由探测记录区分「源挂了」与「解析器坏了」。空结果必带 reason（blocked / upstream /
unreachable / not_ingested），绝不编造数据。

## 存储

参照系库是可重建缓存，v1 用 SQLite 单文件（`FDZX_REF_DB`，默认
`~/.fd-zhengxin/reference.db`），与个人层（政心产品库）物理分库。快照走内容寻址的
不可变存储（`FDZX_SNAPSHOT_DIR`，默认 `~/.fd-zhengxin/snapshots`）。schema 中西无感：
概念/思潮/关系/指标均为开放词表，坐标轴集与指标集随源落定。

## 实体覆盖率（先量再信）

`entity_dossier` 的「查得到」与「工具存在」是两件事：`query_relations` 对不在快照
种子邻域内的 QID 一律 `not_ingested`。探针把三态分别计数（**覆盖率低不是失败**，
测量失败才是）：

```bash
python3 scripts/coverage_report.py                      # 种子 40 人物
python3 scripts/coverage_report.py --qids Q15031 Q16977 # 追加近现代实体
python3 scripts/coverage_report.py --json               # 机器可读
```

2026-11-21 生产实测：种子 40/40 可用；**Wikidata 政治面 0 行**（无快照），且参照系
pod 到 `query.wikidata.org` 不可达——Wikidata 摄取只能在集群外跑完再同步
`reference.db`。详见 `openspec/changes/zhengxin-entity-and-impact-loops/reports/`。

## 本地开发

```bash
pip install -e ".[dev]"
pytest
python3 scripts/check_seed.py    # 先秦种子结构契约自检（只读，CI 可复用）
fd-zhengxin-reference serve --port 8000   # /mcp + /healthz
```

摄取出站代理：`FDZX_REF_PROXY`（空=直连；本地维基数据/维基百科被网络层
拦截时设代理，见 `docs/china-modern-sources.md` 网络实况节）。
