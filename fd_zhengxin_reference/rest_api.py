"""个人层只读 REST 锚点面：GET /api/v1/regions 与 GET /api/v1/concepts/{topic}/anchor。

与 MCP 工具面（/mcp）同库不同面（D3 修复）：主服务个人层消费面
（fd-zhengxin 的 reference_client.py：学习地图坐标系 + 复述校验前提锚）要的是
低频只读 GET JSON，不必走 MCP 会话；本面从库内 currents/concepts 出数
（curated 层启动钩子已物化，断网可查），不触碰 MCP 面。

形态纪律（与主服务 reference_client 的宽容解析逐点对齐）：
- regions：``{"regions": [{"name": …, …}]}`` 裸列表键 + 条目 name 字段；
- anchor：``{"key_points": [str, …]}`` 字符串条目（主服务按 text/premise/name
  取值），种子概念的 key_points = 一句话通说界定 + 原典锚（snippet 按 ｜ 切分）；
- 空结果必带 reason（errors taxonomy）+ source_status 分项（失败口径可见），
  绝不静默为空；条目与键名不做花式嵌套（宽容解析两头都认）。
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import china_lineage, schema, snapshot
from .config import Settings
from .errors import NOT_INGESTED, empty_result
from .provenance import provenance


def _curated_status(conn) -> dict:
    """curated 两源的轻量台账（regions/anchor 响应的 source_status 分项）。"""
    out: dict = {}
    for source, dataset in (
        ("china-seed", china_lineage.SEED_DATASET),
        ("modern-index", china_lineage.MODERN_DATASET),
    ):
        snap = snapshot.latest(conn, dataset)
        if snap:
            out[source] = {"status": "ok", "snapshot": snap["snapshot_id"]}
        else:
            out[source] = {
                "status": "not_ingested",
                "reason": "not_ingested",
                "hint": "curated 层未加载（启动钩子 bootstrap_curated 未执行或 seed 缺失）",
            }
    return out


def list_regions(settings: Settings) -> dict:
    """坐标系区域表：先秦八学派（currents 种子 school 行）+ 近现代五思潮。

    学习地图的「学派/思潮维度」：区域名即 currents 标签（儒家/道家/…/维新
    变法思潮/…），空表必带 reason（curated 层未加载时失败口径可见）。
    """
    conn = settings.connect()
    try:
        schema.init(conn)
        rows = conn.execute(
            "SELECT current_id, label, kind FROM currents"
            " WHERE current_id LIKE ? OR current_id LIKE ?"
            " ORDER BY current_id",
            (china_lineage.SEED_ID_PREFIX + "school/%",
             china_lineage.MODERN_ID_PREFIX + "%"),
        ).fetchall()
        status = _curated_status(conn)
        snap = snapshot.latest(conn, china_lineage.SEED_DATASET)
    finally:
        conn.close()
    if not rows:
        return empty_result(
            NOT_INGESTED,
            hint="库内无学派/思潮维度（curated 层未加载：启动钩子 bootstrap_curated）",
            regions=[], source_status=status,
        )
    return {
        "regions": [
            {"name": r["label"], "kind": r["kind"], "id": r["current_id"]}
            for r in rows
        ],
        "source_status": status,
        "provenance": provenance(
            "china-seed",
            stable_id="currents:regions",
            snapshot_version=snap["snapshot_id"] if snap else None,
        ),
    }


def concept_anchor(topic: str, settings: Settings) -> dict:
    """概念前提锚：topic → 关键前提列表（复述校验的前提集来源之一）。

    命中顺序（都只读库内 curated 行，精确匹配优先、其次最长子串）：
    1. 种子概念（concepts 表 #china-seed 行，term 精确或为 topic 的最长子串）：
       key_points = snippet 按 ｜ 切分（一句话通说界定 + 原典篇目锚）；
    2. 近现代思潮（currents modern:china/ 标签精确或子串）：key_points = [summary]；
    3. 未命中：key_points 空 + reason not_ingested + hint（不猜前提）。
    """
    term = " ".join(str(topic or "").split())
    conn = settings.connect()
    try:
        schema.init(conn)
        status = _curated_status(conn)
        if not term:
            return empty_result(
                NOT_INGESTED, hint="空主题：无可对照的概念锚",
                key_points=[], source_status=status,
            )

        # 1) 种子概念：精确命中，否则取 topic 内包含的最长种子标签（仍是库内
        #    curated 词条的原文，不造前提）
        row = conn.execute(
            "SELECT term, title, url, snippet FROM concepts"
            " WHERE term=? AND url LIKE ? ORDER BY rowid LIMIT 1",
            (term, "%" + china_lineage.SEED_CONCEPT_MARKER),
        ).fetchone()
        if row is None:
            for cand in conn.execute(
                "SELECT term, title, url, snippet FROM concepts"
                " WHERE url LIKE ? ORDER BY LENGTH(term) DESC",
                ("%" + china_lineage.SEED_CONCEPT_MARKER,),
            ).fetchall():
                if cand["term"] in term:
                    row = cand
                    break
        if row is not None:
            key_points = [p.strip() for p in (row["snippet"] or "").split("｜") if p.strip()]
            snap = snapshot.latest(conn, china_lineage.SEED_DATASET)
            return {
                "key_points": key_points,
                "term": row["term"],
                "source": "china-seed",
                "source_status": status,
                "provenance": provenance(
                    "china-seed",
                    stable_id=f"china-seed:concept:{row['term']}",
                    snapshot_version=snap["snapshot_id"] if snap else None,
                ),
            }

        # 2) 近现代思潮：标签精确或子串（思潮本体即坐标节点）
        mrow = conn.execute(
            "SELECT current_id, label FROM currents"
            " WHERE current_id LIKE ? AND label=? LIMIT 1",
            (china_lineage.MODERN_ID_PREFIX + "%", term),
        ).fetchone()
        if mrow is None:
            for cand in conn.execute(
                "SELECT current_id, label FROM currents"
                " WHERE current_id LIKE ? ORDER BY LENGTH(label) DESC",
                (china_lineage.MODERN_ID_PREFIX + "%",),
            ).fetchall():
                if cand["label"] in term:
                    mrow = cand
                    break
        if mrow is not None:
            summary = None
            manifest = snapshot.latest(conn, china_lineage.MODERN_DATASET)
            if manifest:
                payload = json.loads(snapshot.read(manifest))
                for c in payload.get("currents", []):
                    if c["label"] == mrow["label"]:
                        summary = c.get("summary")
                        break
            key_points = [summary] if summary else []
            if not key_points:
                return empty_result(
                    NOT_INGESTED,
                    hint=f"思潮 {mrow['label']!r} 命中但快照无摘要，无可对照前提",
                    key_points=[], source_status=status,
                )
            return {
                "key_points": key_points,
                "term": mrow["label"],
                "source": "modern-index",
                "source_status": status,
                "provenance": provenance(
                    "modern-index",
                    stable_id=mrow["current_id"],
                    snapshot_version=manifest["snapshot_id"] if manifest else None,
                ),
            }

        return empty_result(
            NOT_INGESTED,
            hint=f"库内概念锚无 {term!r}（种子 50 概念精确/子串 + 近现代五思潮标签；"
                 "不在库内即无可对照前提，不猜测）",
            key_points=[], source_status=status,
        )
    finally:
        conn.close()


def add_routes(app, settings: Settings) -> None:
    """把两个只读端点挂到既有 ASGI app（cli.serve 的 http_app + /healthz 同形）。"""

    async def regions_endpoint(_: Request) -> JSONResponse:
        return JSONResponse(list_regions(settings))

    async def anchor_endpoint(request: Request) -> JSONResponse:
        topic = request.path_params.get("topic", "")
        return JSONResponse(concept_anchor(topic, settings))

    app.routes.append(Route("/api/v1/regions", regions_endpoint, methods=["GET"]))
    app.routes.append(Route("/api/v1/concepts/{topic}/anchor", anchor_endpoint, methods=["GET"]))
