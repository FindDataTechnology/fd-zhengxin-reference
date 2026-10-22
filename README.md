# fd-zhengxin-reference

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
| `source_status` | 六源摄取状态与缺口（未摄取必带原因） | 摄取台账表 + 许可门 | 已接线 |

启动初始化：`create_server` 以启动钩子形态幂等加载 curated 层（先秦种子谱系
`china-seed` + 近现代五思潮索引 `modern-index`，需 schema.init 后调用）；DB 为
`:memory:` 时跳过，seed 文件缺失时跳过并如实回报（缺口在 `source_status` 可见）。

## 源清单与许可门纪律

| 源 | 用途 | 许可门 |
|---|---|---|
| V-Dem | 政体指标时序（v2x_* 等指数） | 已确认（CC BY-SA 4.0，非 CC BY；ShareAlike 只在导出面触发，摄取限定 Core/Country-Date 变体） |
| MARPOR | 政党宣言立场编码（rile 等） | 评审中（三点未确认：注册流程 / 科研用途边界 / 再分发授权；未过门不摄取） |
| Wikidata | 全量层对齐（QID 回填） | 已确认（CC0） |
| SEP | 概念引用层（只链不抄；政治思想词 26/26 命中实测） | 已确认（站点条款） |
| 中国线 | 政治思想词条/人物/关系 | 评审中（源清单待定；未过门不摄取） |

许可证据（`docs/data-license.md`，2026-10-23 官网一手核验；另有
`docs/sep-coverage.md` 覆盖面实测与 `docs/wikidata-probe-samples/` SPARQL 探针样本）。

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

## 本地开发

```bash
pip install -e ".[dev]"
pytest
fd-zhengxin-reference serve --port 8000   # /mcp + /healthz
```
