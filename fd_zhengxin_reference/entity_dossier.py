"""政治实体读面：单实体 → 身份 + 学派 + 出入关系边全集 + 覆盖三态。

存在理由（change zhengxin-entity-and-impact-loops，见其 design.md D1/D2）：
`relation_query(source, target)` 把关系面收敛成了两两配对，因此跑不出「这个人的
全部关系」——而 `wikidata_politics.query_relations(conn, qid)` 的第一参数本来就是
单 QID、SQL 是 `WHERE source_id=? OR target_id=?`，单实体全边能力一直在，只是没
有调用面。本模块就是那层调用面。

两个数据面（双来源融合，逐行 source 标注）：
- `china-seed`：先秦 40 人物 + 83 条 seed 边（curated；`currents` / `relations` 表）；
- `wikidata`：`wd_political_entities` 四类实体 + `wd_political_relations`（CC0）。

覆盖三态（经 `coverage` 字段进响应第一层，不进 gaps）：
- `available`     实体在当前快照内，关系边已尽力召回；
- `not_in_snapshot` 该数据面有快照，但此实体不在其中（补摄取这个实体即可解）；
- `not_ingested`  该数据面压根没有快照（要先跑整个摄取）。
两态分开是硬要求：合并会让运维按错误方向修。

实测事实（reports/baseline-2026-11-21.md）：生产参照系的 wikidata 面为 0 行，且
参照系 pod 到 query.wikidata.org 不可达。因此**当前生产的实体读面实际只有
china-seed 一支**；wikidata 支返回 not_ingested 是如实回报，不是缺陷。

失败口径照 errors.py：未命中必带 reason，绝不编造关系。
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import china_lineage, schema, snapshot
from . import wikidata_politics as wpx
from .config import Settings
from .errors import NOT_INGESTED, empty_result
from .provenance import provenance

WD_DATASET = "wikidata-politics"

# 覆盖三态（进响应第一层，语义见模块 docstring）
COVERAGE_AVAILABLE = "available"
COVERAGE_NOT_IN_SNAPSHOT = "not_in_snapshot"
COVERAGE_NOT_INGESTED = "not_ingested"


def _wd_snapshot_id(conn) -> str | None:
    """wikidata 面最近快照 id（无 = 未摄取）。"""
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE dataset=? ORDER BY fetched_at DESC LIMIT 1",
        (WD_DATASET,),
    ).fetchone()
    return row["snapshot_id"] if row else None


def _seed_snapshot_id(conn) -> str | None:
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE dataset=? ORDER BY fetched_at DESC LIMIT 1",
        (china_lineage.SEED_DATASET,),
    ).fetchone()
    return row["snapshot_id"] if row else None


# ---------------------------------------------------------------- 解析层

# 繁→简 姓名高频字映射（不做通用繁简转换；见 _simplify 的 shortcut 说明）
_TRAD_TO_SIMP = str.maketrans({
    "鄧": "邓", "國": "国", "黨": "党", "東": "东", "華": "华", "學": "学",
    "經": "经", "濟": "济", "體": "体", "會": "会", "議": "议", "長": "长",
    "張": "张", "劉": "刘", "陳": "陈", "黃": "黄", "楊": "杨", "趙": "赵",
    "週": "周", "吳": "吴", "鄭": "郑", "孫": "孙", "馬": "马", "葉": "叶",
    "蘇": "苏", "許": "许", "呂": "吕", "曾": "曾", "謝": "谢", "唐": "唐",
    "馮": "冯", "於": "于", "蕭": "萧", "羅": "罗", "錢": "钱", "陸": "陆",
    "陽": "阳", "陰": "阴", "開": "开", "關": "关", "門": "门", "龍": "龙",
    "鳳": "凤", "齊": "齐", "魯": "鲁", "衛": "卫", "晉": "晋", "漢": "汉",
    "戰": "战", "紅": "红", "軍": "军", "產": "产", "業": "业", "農": "农",
    "後": "后", "復": "复", "興": "兴", "榮": "荣", "偉": "伟", "澤": "泽",
    "東": "东", "來": "来", "樂": "乐", "義": "义", "禮": "礼", "賢": "贤",
    "習": "习", "總": "总", "書": "书", "記": "记", "務": "务", "員": "员",
    "機": "机", "構": "构", "幹": "干", "部": "部", "縣": "县", "區": "区",
    "鄉": "乡", "鎮": "镇", "廳": "厅", "處": "处", "辦": "办", "實": "实",
    "鹽": "盐", "鐵": "铁", "銀": "银", "鋼": "钢", "車": "车", "馬": "马",
})


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _simplify(text: str) -> str:
    """繁体 → 简体（姓名高频字子集）。

    shortcut: 只覆盖姓名里高频的繁体字；生僻字不在表里就命中不了，回退成
    「查不到」而不是猜。要全量繁简需引入 opencc（一个依赖进每个消费者环境），
    当前不值得——中文人名查询的实际命中率靠这张表已足够。
    """
    return text.translate(_TRAD_TO_SIMP)


def _resolve(conn, ref: str) -> tuple[dict, dict | None]:
    """实体引用 → (resolution 结构, 命中说明)。

    resolution.status ∈ found | not_found | ambiguous。歧义时返回候选清单而不择一
    （`_resolve_wd_entity` 的 `LIMIT 1` 静默择一，这里必须显式拒选）。
    """
    n = (ref or "").strip()
    if not n:
        return {"status": "not_found", "reason": "empty_ref"}, None

    candidates: list[dict] = []

    # 1) QID 直填：先看是否命中种子人物自带 qid（种子 40 人只 14 人有 qid），
    #    再记为 wikidata 面候选（存在性交给下面的快照判定）。
    if wpx.QID_RE.fullmatch(n):
        payload = china_lineage.seed_snapshot_payload(conn) or {}
        for f in payload.get("figures") or []:
            if f.get("qid") == n:
                candidates.append({
                    "face": "china-seed",
                    "current_id": f"{china_lineage.SEED_ID_PREFIX}thinker/{f['id']}",
                    "label": f.get("label"), "kind": "thinker",
                })
                break
        if not candidates:
            # 同一实体两面都有时 curated 胜出（带 school/years，wikidata 行缺），
            # 因此只在种子未命中时才把 QID 记为 wikidata 面候选
            candidates.append({"face": "wikidata", "qid": n, "label": None, "kind": None})

    # 2) currents（种子诗人/学派/思潮）标签或 current_id
    seed_rows = conn.execute(
        "SELECT current_id, label, kind FROM currents"
        " WHERE current_id LIKE ? AND (current_id=? OR label=? COLLATE NOCASE)",
        (china_lineage.SEED_ID_PREFIX + "%", n, n),
    ).fetchall()
    modern_rows = conn.execute(
        "SELECT current_id, label, kind FROM currents"
        " WHERE current_id LIKE ? AND (current_id=? OR label=? COLLATE NOCASE)",
        (china_lineage.MODERN_ID_PREFIX + "%", n, n),
    ).fetchall()
    for r in list(seed_rows) + list(modern_rows):
        candidates.append({
            "face": "china-seed" if r["current_id"].startswith(china_lineage.SEED_ID_PREFIX)
                     else "modern-index",
            "current_id": r["current_id"], "label": r["label"], "kind": r["kind"],
        })

    # 3) wikidata 实体标签（仅当已摄取）
    if _wd_snapshot_id(conn):
        wd_rows = conn.execute(
            "SELECT qid, kind, label_en, label_zh FROM wd_political_entities"
            " WHERE label_en=? COLLATE NOCASE OR label_zh=? COLLATE NOCASE",
            (n, n),
        ).fetchall()
        if not wd_rows:
            # 简繁兜底：Wikidata 的 zh 标签格式不统一（实测 2026-11-22，49 个
            # 中文标签里 8 个是繁体，如「鄧小平」），而用户大概率敲简体。
            # 标准库没有繁简转换、也不为此引依赖——把**库侧标签**归一后与查询词
            # 比对（不是转换查询词：简体查询词转不回繁体）。只扫 label_zh 非空的
            # 行，量级是千级，可接受。
            # shortcut: 只覆盖姓名常见字；生僻字命中不了 → 回退成「查不到」，
            # 不猜。要全量繁简需引入 opencc，届时换掉本段。
            target = _simplify(n)
            if target != n or _has_cjk(n):
                wd_rows = [
                    r for r in conn.execute(
                        "SELECT qid, kind, label_en, label_zh FROM wd_political_entities"
                        " WHERE label_zh IS NOT NULL AND label_zh != ''"
                    ).fetchall()
                    if _simplify(r["label_zh"]) == target
                ]
        for r in wd_rows:
            candidates.append({
                "face": "wikidata", "qid": r["qid"], "kind": r["kind"],
                "label": r["label_zh"] or r["label_en"],
            })

    # 去重（QID 直填与标签命中会指向同一实体）
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for c in candidates:
        key = (c["face"], c.get("qid") or c.get("current_id"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    if not uniq:
        return {"status": "not_found", "reason": "no_match"}, None
    if len(uniq) > 1:
        return {"status": "ambiguous", "candidates": uniq}, None
    return {"status": "found", **uniq[0]}, uniq[0]


# ---------------------------------------------------------------- 两个数据面各自的读面


def _seed_dossier(conn, hit: dict) -> dict:
    """种子/近现代实体：身份 + 学派 + 出入边（双向全量，不走两两配对）。"""
    current_id = hit["current_id"]
    schools, figures_map, concepts_map, _edges = china_lineage.seed_maps(
        china_lineage.seed_snapshot_payload(conn)
    )
    fid = china_lineage.figure_id_of(current_id)
    fig = figures_map.get(fid) or {}

    rows = conn.execute(
        "SELECT source_id, target_id, kind, polarity, snapshot_version FROM relations"
        " WHERE source_id=? OR target_id=? ORDER BY rowid",
        (current_id, current_id),
    ).fetchall()

    out_edges, in_edges = [], []
    for r in rows:
        direction = "out" if r["source_id"] == current_id else "in"
        other_id = r["target_id"] if direction == "out" else r["source_id"]
        other = figures_map.get(china_lineage.figure_id_of(other_id)) or {}
        edge = {
            "direction": direction,
            "other": other.get("label") or china_lineage.figure_id_of(other_id),
            "other_id": other_id,
            "kind": r["kind"],
            "polarity": r["polarity"],
            "other_school": schools.get(other.get("school")),
            "ref": f"china-seed:relation:{r['source_id']}>{r['target_id']}",
            "snapshot_version": r["snapshot_version"],
        }
        (out_edges if direction == "out" else in_edges).append(edge)

    return {
        "identity": {
            "ref": current_id,
            "label": hit.get("label") or current_id,
            "kind": hit.get("kind"),
            "school": schools.get(fig.get("school")),
            "years": fig.get("years"),
            "era": fig.get("era"),
        },
        "relations": {"out": out_edges, "in": in_edges},
        "relation_count": len(rows),
    }


def _wd_dossier(conn, qid: str) -> tuple[dict | None, dict]:
    """wikidata 实体：身份 + 出入边。返回 (dossier | None, coverage 结构)。"""
    snap = _wd_snapshot_id(conn)
    if snap is None:
        return None, {
            "status": COVERAGE_NOT_INGESTED,
            "detail": (
                "wikidata 政治面无快照（生产实测 0 行；参照系 pod 到 query.wikidata.org"
                " 亦不可达，摄取须在集群外跑，见 reports/baseline-2026-11-21.md）"
            ),
        }
    row = conn.execute(
        "SELECT qid, kind, label_en, label_zh, birth, death, inception,"
        " point_in_time, country, class_label FROM wd_political_entities"
        " WHERE qid=? AND snapshot_version=?",
        (qid, snap),
    ).fetchone()
    if row is None:
        return None, {
            "status": COVERAGE_NOT_IN_SNAPSHOT,
            "detail": (
                f"{qid} 不在 wikidata 政治面当前快照 {snap}"
                "（补摄取该 QID 的种子邻域即可解）"
            ),
        }

    rel = wpx.query_relations(conn, qid)
    out_edges, in_edges = [], []
    for e in rel.get("results") or []:
        direction = e.get("direction") or (
            "out" if e["source_id"] == qid else "in"
        )
        edge = {
            "direction": direction,
            "other": e.get("label_target") if direction == "out" else e.get("label_source"),
            "other_id": e["target_id"] if direction == "out" else e["source_id"],
            "kind": e.get("kind"),
            "pid": e.get("property_pid"),
            "polarity": e.get("polarity"),
            "weight": e.get("weight"),
            "ref": (e.get("provenance") or {}).get("stable_id"),
            "snapshot_version": e.get("snapshot_version"),
        }
        (out_edges if direction == "out" else in_edges).append(edge)

    anchors = [
        {"pid": a["anchor_pid"], "label": a["anchor_label"]}
        for a in conn.execute(
            "SELECT anchor_pid, anchor_label FROM wd_ideology_anchors"
            " WHERE qid=? AND snapshot_version=?",
            (qid, snap),
        ).fetchall()
    ]

    dossier = {
        "identity": {
            "ref": qid,
            "label": row["label_zh"] or row["label_en"],
            "kind": row["kind"],
            "birth": row["birth"],
            "death": row["death"],
            "inception": row["inception"],
            "point_in_time": row["point_in_time"],
            "class_label": row["class_label"],
            "country": row["country"],
        },
        "relations": {"out": out_edges, "in": in_edges},
        "relation_count": len(out_edges) + len(in_edges),
        "ideology_anchors": anchors,
    }
    return dossier, {"status": COVERAGE_AVAILABLE, "detail": None}


# ---------------------------------------------------------------- 注册


def register(mcp: FastMCP, settings: Settings) -> None:
    """挂 `entity_dossier` 工具（闭包绑定 settings）。"""

    async def entity_dossier(ref: str) -> dict:
        """政治实体读面：人物/政党/事件/运动/学派/思潮 → 身份 + 学派 + 出入关系边全集。

        ref 可为 Wikidata QID、curated 实体 id（seed:china/… / modern:china/…）或
        标签（精确匹配）。返回 coverage 三态进第一层（available / not_in_snapshot /
        not_ingested）；标签歧义返回候选清单不择一；未命中必带 reason，绝不编造边。

        与 relation_query 的区别：那是两两配对查询，这是单实体全边——「这人的全部
        关系」只有本工具能答。
        """
        conn = settings.connect()
        try:
            schema.init(conn)
            wpx.ensure_tables(conn)
            resolution, hit = _resolve(conn, ref)
        finally:
            conn.close()

        if resolution["status"] == "not_found":
            failed = empty_result(
                NOT_INGESTED,
                hint=(
                    f"{ref!r} 未命中任何实体面（QID / curated id / 标签精确匹配）。"
                    "先秦 40 人物用全名标签（如「孔子」「韩非」）；wikidata 面需先摄取。"
                ),
            )
            return {**failed, "resolution": resolution, "coverage": {
                "status": COVERAGE_NOT_INGESTED, "detail": "无可解析的实体面",
            }}
        if resolution["status"] == "ambiguous":
            return {
                "results": [],
                "reason": NOT_INGESTED,
                "hint": f"{ref!r} 在多个实体面命中，请用更具体的引用（QID 或完整 id）",
                "resolution": resolution,
                "coverage": {"status": COVERAGE_NOT_IN_SNAPSHOT, "detail": "歧义未解析"},
            }

        conn = settings.connect()
        try:
            schema.init(conn)
            wpx.ensure_tables(conn)
            face = hit["face"]
            if face == "wikidata":
                dossier, coverage = _wd_dossier(conn, hit["qid"])
                if dossier is None:
                    return {
                        "results": [], "reason": NOT_INGESTED,
                        "hint": coverage.get("detail"),
                        "resolution": resolution, "coverage": coverage,
                    }
                src_snap = _wd_snapshot_id(conn)
                dossier["provenance"] = provenance(
                    "wikidata", stable_id=f"wikidata-politics:{hit['qid']}",
                    snapshot_version=src_snap,
                )
            else:
                dossier = _seed_dossier(conn, hit)
                dossier["provenance"] = provenance(
                    hit["face"], stable_id=hit["current_id"],
                    snapshot_version=_seed_snapshot_id(conn),
                )
                coverage = {"status": COVERAGE_AVAILABLE, "detail": None}
        finally:
            conn.close()

        return {
            **dossier,
            "resolution": resolution,
            "coverage": coverage,
            "gaps": [] if coverage["status"] == COVERAGE_AVAILABLE else [
                {"stage": "coverage", "reason": coverage["status"], "detail": coverage["detail"]},
            ],
        }

    mcp.tool(entity_dossier)


__all__ = ["register", "COVERAGE_AVAILABLE", "COVERAGE_NOT_IN_SNAPSHOT", "COVERAGE_NOT_INGESTED"]
