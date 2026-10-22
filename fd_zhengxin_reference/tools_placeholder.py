"""四类查询工具——接线形态（模块历史名保留，工具名与注册模式不变）。

四个工具与喂源模块的接线（多源融合：逐行 source 标注 + 逐结果 provenance，
sources 分项如实记录各源状态）：

- concept_lookup     概念：china_lineage 种子概念（concepts 表 #china-seed 行）+
                     近现代五思潮索引（currents + 不可变快照）先查库内，再
                     sep_politics.query_concept（缓存→实时→curated 兜底）
- ideology_query     思潮坐标：wikidata_politics.query_ideology_anchor
                     （P1142 意识形态 / P1387 政治倾向）+ marpor.
                     query_marpor_timeseries（门关 → not_ingested +
                     gate_status=license_unconfirmed，两层口径不混用，见 errors.py）
- relation_query     关系边：wikidata_politics.query_relations（polarity 直通）
                     + china_lineage 种子边（relations 表 seed:china/ 命名空间）
- regime_timeseries  政体时序：vdem.query_regime_timeseries 直通（含指标口径说明）

失败纪律照 errors.py：空结果必带 reason（taxonomy），绝不静默为空、绝不编造；
好源不受坏源影响——任一源断（不可达/门关/无数据）只进自己的 sources 分项。
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from . import china_lineage, marpor, schema, sep_politics, vdem, wikidata_politics
from .config import Settings
from .errors import NOT_INGESTED, empty_result
from .provenance import provenance
from . import snapshot


# ---------------------------------------------------------------- 多源融合

def _leg_summary(leg: dict) -> dict:
    """单源分项摘要：成功 {status: ok, rows}（curated 兜底另带 note）；
    空/失败保留 reason/hint 与台账补充字段（gate_status 等）。"""
    if leg.get("results"):
        out = {"status": "ok", "rows": len(leg["results"])}
        if leg.get("note"):
            out["note"] = leg["note"]
        return out
    keep = (k for k in ("reason", "hint", "gate_status", "gate_detail") if leg.get(k) is not None)
    return {"status": "miss", **{k: leg[k] for k in keep}}


def _fuse(legs: dict[str, dict], what: str) -> dict:
    """多源融合：逐行并入 results（行内标注 source），sources 分项记录各源状态。

    全空时 reason 取首个非 not_ingested 的失败口径（unreachable/upstream 优先于
    「没数据」），否则 not_ingested；hint 汇总各源说明——绝不静默为空。
    """
    rows: list[dict] = []
    for source, leg in legs.items():
        rows.extend({"source": source, **row} for row in leg.get("results", []))
    sources = {s: _leg_summary(leg) for s, leg in legs.items()}
    if rows:
        return {"results": rows, "sources": sources}
    reason = next(
        (leg["reason"] for leg in legs.values() if leg.get("reason") not in (None, NOT_INGESTED)),
        NOT_INGESTED,
    )
    hints = [f"{s}：{leg['hint']}" for s, leg in legs.items() if leg.get("hint")]
    return empty_result(reason, hint="；".join(hints) or f"{what}各源均无结果", sources=sources)


# ---------------------------------------------------------------- 名字解析

def _resolve_wd_entity(conn, name: str) -> str | None:
    """wikidata 面：QID 直填，或当前快照实体标签精确匹配（大小写不敏感）。"""
    n = name.strip()
    if wikidata_politics.QID_RE.fullmatch(n):
        return n
    row = conn.execute(
        "SELECT qid FROM wd_political_entities"
        " WHERE label_en=? COLLATE NOCASE OR label_zh=? COLLATE NOCASE LIMIT 1",
        (n, n),
    ).fetchone()
    return row["qid"] if row else None


def _resolve_seed_entity(conn, name: str) -> str | None:
    """种子谱系：current_id 直填（seed:china/…）或标签精确匹配（学派/人物）。"""
    n = name.strip()
    row = conn.execute(
        "SELECT current_id FROM currents"
        " WHERE current_id LIKE ? AND (current_id=? OR label=? COLLATE NOCASE) LIMIT 1",
        (china_lineage.SEED_ID_PREFIX + "%", n, n),
    ).fetchone()
    return row["current_id"] if row else None


# ---------------------------------------------------------------- concept_lookup 各源

def _seed_concept_leg(query: str, settings: Settings) -> dict:
    """库内种子概念（concepts 表 #china-seed 行；启动钩子物化，离线可查）。"""
    conn = settings.connect()
    try:
        schema.init(conn)
        rows = conn.execute(
            "SELECT term, title, url, snippet, retrieved_at FROM concepts"
            " WHERE term=? AND url LIKE ? ORDER BY rowid",
            (query, "%" + china_lineage.SEED_CONCEPT_MARKER),
        ).fetchall()
        snap = snapshot.latest(conn, china_lineage.SEED_DATASET)
    finally:
        conn.close()
    if not rows:
        return empty_result(
            NOT_INGESTED,
            hint=f"库内种子概念无 {query!r}（先秦谱系 50 概念，精确标签匹配）",
        )
    sid = snap["snapshot_id"] if snap else None
    return {
        "results": [
            {
                "term": r["term"],
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"],
                "retrieved_at": r["retrieved_at"],
                "provenance": provenance(
                    "china-seed",
                    stable_id=f"china-seed:concept:{query}",
                    snapshot_version=sid,
                ),
            }
            for r in rows
        ]
    }


def _modern_concept_leg(query: str, settings: Settings) -> dict:
    """近现代五思潮索引（currents 标签精确匹配；摘要与文献条目读自不可变快照）。"""
    conn = settings.connect()
    try:
        schema.init(conn)
        row = conn.execute(
            "SELECT current_id, label, snapshot_version FROM currents"
            " WHERE current_id LIKE ? AND label=? COLLATE NOCASE LIMIT 1",
            (china_lineage.MODERN_ID_PREFIX + "%", query),
        ).fetchone()
        manifest = snapshot.latest(conn, china_lineage.MODERN_DATASET)
    finally:
        conn.close()
    if row is None:
        return empty_result(
            NOT_INGESTED,
            hint=f"近现代五思潮索引无 {query!r}（按思潮全称精确匹配，如「维新变法思潮」）",
        )
    entry: dict = {"label": row["label"], "summary": None, "documents": []}
    if manifest:
        payload = json.loads(snapshot.read(manifest))
        for c in payload.get("currents", []):
            if c["label"] == row["label"]:
                entry["summary"] = c.get("summary")
                entry["documents"] = [
                    {k: d.get(k) for k in ("title", "author", "year", "url", "form")}
                    for d in c.get("documents", [])
                ]
                break
    return {
        "results": [
            {
                **entry,
                "provenance": provenance(
                    "modern-index",
                    stable_id=row["current_id"],
                    snapshot_version=manifest["snapshot_id"] if manifest else row["snapshot_version"],
                ),
            }
        ]
    }


async def _sep_concept_leg(query: str, settings: Settings) -> dict:
    """SEP 概念层（缓存 TTL→实时搜索→curated 兜底，模块自治；行附模块 provenance）。"""
    out = await sep_politics.query_concept(query, settings=settings)
    if not out.get("results"):
        return out  # empty_result（unreachable/upstream），模块已带口径与 hint
    prov = out.get("provenance")
    leg = {"results": [{**hit, "provenance": prov} for hit in out["results"]]}
    if out.get("note"):  # curated 离线锚点时的诚实标注（随 sources 分项透出）
        leg["note"] = out["note"]
    return leg


# ---------------------------------------------------------------- relation_query 各源

def _wd_pair_leg(conn, src_name: str, tgt_name: str, src_qid: str | None,
                 tgt_qid: str | None, polarity: str | None) -> dict:
    """wikidata 关系边：query_relations（极性过滤直通）后再按端点对收敛。"""
    if src_qid is None or tgt_qid is None:
        missing = [n for n, q in ((src_name, src_qid), (tgt_name, tgt_qid)) if q is None]
        return empty_result(
            NOT_INGESTED,
            hint=f"{'、'.join(repr(m) for m in missing)} 未命中 wikidata 政治面"
            "（QID 直填或 label 精确匹配；未摄取或不在种子邻域内）",
        )
    out = wikidata_politics.query_relations(conn, src_qid, polarity)
    if not out.get("results"):
        return out  # 未摄取/无该极性边：模块已带 reason/hint
    pair = {(src_qid, tgt_qid), (tgt_qid, src_qid)}
    rows = [r for r in out["results"] if (r["source_id"], r["target_id"]) in pair]
    if not rows:
        scope = f"极性 {polarity} 的" if polarity else ""
        return empty_result(
            NOT_INGESTED,
            hint=f"{src_qid} 与 {tgt_qid} 均已摄取，但两者之间当前快照无{scope}边"
            "（种子邻域一跳/二跳内无边）",
        )
    return {"results": rows}


def _seed_pair_leg(conn, src_name: str, tgt_name: str, src_id: str | None,
                   tgt_id: str | None) -> dict:
    """种子关系边：师承/批判/论战（relations 表 seed:china/ 命名空间，双向查）。"""
    if src_id is None or tgt_id is None:
        missing = [n for n, i in ((src_name, src_id), (tgt_name, tgt_id)) if i is None]
        return empty_result(
            NOT_INGESTED,
            hint=f"{'、'.join(repr(m) for m in missing)} 未命中先秦种子谱系"
            "（40 人物精确标签匹配；china-line 语料层许可评审中，种子层之外无关系数据）",
        )
    rows = conn.execute(
        "SELECT source_id, target_id, kind, polarity, snapshot_version FROM relations"
        " WHERE (source_id=? AND target_id=?) OR (source_id=? AND target_id=?)"
        " ORDER BY rowid",
        (src_id, tgt_id, tgt_id, src_id),
    ).fetchall()
    if not rows:
        return empty_result(
            NOT_INGESTED,
            hint=f"{src_name!r} 与 {tgt_name!r} 均在种子谱系，但两者之间无边"
            "（种子边集为 83 条师承/批判/论战，覆盖所录 40 人物）",
        )
    return {
        "results": [
            {
                **{k: r[k] for k in r.keys()},
                "provenance": provenance(
                    "china-seed",
                    stable_id=f"china-seed:relation:{r['source_id']}>{r['target_id']}",
                    snapshot_version=r["snapshot_version"],
                ),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------- 注册（闭包绑定 settings）

def register(mcp: FastMCP, settings: Settings) -> None:
    """挂工具：闭包绑定 settings（每个 server 实例各自的 DB）。"""

    async def concept_lookup(term: str) -> dict:
        """概念查询：政治概念的定义摘要与引用链接（只链不抄，正文不落库）。
        先查库内（中国线种子概念 + 近现代五思潮索引），再 SEP 实时（缓存 TTL→
        实时搜索→curated 兜底）；融合返回，逐行标注 source（china-seed /
        modern-index / sep）并附 provenance，sources 分项记录各源状态。空结果
        必带 reason，绝不编造。"""

        query = " ".join(term.split()).lower()
        if not query:
            raise ValueError("空查询词")
        legs = {
            "china-seed": _seed_concept_leg(query, settings),
            "modern-index": _modern_concept_leg(query, settings),
            "sep": await _sep_concept_leg(query, settings),
        }
        return _fuse(legs, what=f"概念 {query!r}")

    async def ideology_query(name: str) -> dict:
        """思潮坐标查询：思潮/思想家/政党在政治坐标轴上的位置。Wikidata 坐标锚
        双通道（P1142 意识形态 / P1387 政治倾向，name 为 QID 或 label）+
        MARPOR 立场编码；MARPOR 许可门未过（license_unconfirmed）时该源如实
        not_ingested 并附 gate_detail，绝不编造。空结果必带 reason。"""

        conn = settings.connect()
        try:
            wikidata_politics.ensure_tables(conn)
            qid = _resolve_wd_entity(conn, name)
            if qid is None:
                snap = snapshot.latest(conn, wikidata_politics.DATASET)
                wd_leg = empty_result(
                    NOT_INGESTED,
                    hint=f"{name!r} 未命中 wikidata 政治面（QID 直填或 label 精确匹配；"
                    f"当前快照 {'无（未摄取）' if snap is None else snap['snapshot_id']}）",
                )
            else:
                wd_leg = wikidata_politics.query_ideology_anchor(conn, qid)
        finally:
            conn.close()
        legs = {
            "wikidata": wd_leg,
            # 门关时 query_marpor_timeseries 恒空：not_ingested + gate_status=
            # license_unconfirmed（台账口径在分项，不与查询口径混用）
            "marpor": marpor.query_marpor_timeseries(name, settings=settings),
        }
        return _fuse(legs, what=f"思潮坐标 {name!r}")

    async def relation_query(source: str, target: str, polarity: str | None = None) -> dict:
        """关系查询：两个思潮/思想家/政党之间的影响/批判/论战边。Wikidata 关系
        边（query_relations，polarity 直通：inheritance/alliance/opposition/
        critique——critique 档 WD 结构性缺失，由中国线承载）+ 中国线种子边
        （polarity 口径 positive/critical，与 WD 口径不做映射，不编造对应关系）。
        source/target 可为 QID 或标签（各源精确匹配）。空结果必带 reason。"""

        if polarity is not None and polarity not in wikidata_politics.POLARITY_TIERS:
            raise ValueError(
                f"未知极性 {polarity!r}（合法值：{wikidata_politics.POLARITY_TIERS}）"
            )
        conn = settings.connect()
        try:
            wikidata_politics.ensure_tables(conn)
            wd_leg = _wd_pair_leg(
                conn, source, target,
                _resolve_wd_entity(conn, source), _resolve_wd_entity(conn, target),
                polarity,
            )
            seed_leg = _seed_pair_leg(
                conn, source, target,
                _resolve_seed_entity(conn, source), _resolve_seed_entity(conn, target),
            )
        finally:
            conn.close()
        return _fuse(
            {"wikidata": wd_leg, "china-seed": seed_leg},
            what=f"关系 {source!r}→{target!r}",
        )

    async def regime_timeseries(country: str, indicator: str | None = None,
                                years: list[int] | None = None) -> dict:
        """政体时序查询：国家×年份的政体指标序列（V-Dem v2x_* 聚合指数，直通）。
        附指标口径说明（indicator_notes）与 provenance（回到具体快照）；years 为
        显式年份列表（两元素升序视为闭区间）。country 为 country_text_id（如 USA）
        或官方英文国名。空结果必带 reason（未摄取/未命中均有 hint）。"""

        return vdem.query_regime_timeseries(
            country, years, indicator=indicator, settings=settings
        )

    mcp.tool(concept_lookup)
    mcp.tool(ideology_query)
    mcp.tool(relation_query)
    mcp.tool(regime_timeseries)
