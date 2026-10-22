"""Wikidata 政治面摄取器测试：离线 fixture 全链路 + live 冒烟。

离线：docs/wikidata-probe-samples/ 的真实样本 JSON 作响应桩（探针查询姿势与
本模块 builder 的变量名一一对应，fixture 直接回放），覆盖——
- 四类实体解析入库（人物/政党/事件/运动，含 subclass 闭包姿势断言）
- 极性推导（判据表分层：对立 P5004>P461>P7047 权重序；P991 非极性）
- 查询过滤（query_entity / query_relations 极性过滤与双向 / query_ideology_anchor）
- 断网口径（SparqlError → unreachable/upstream，全失败 failed、部分失败 partial，
  绝不静默为空）；快照版本化（摄取时间+查询指纹、CC0 归属、全量刷新）
- 噪声收敛（P39 不入判据，pol2 的「先知」误挂样本不产生任何行）

live 冒烟（pytest.mark.live，默认跳过）：真打一次 WDQS 小 LIMIT 查询。
门控用环境变量 FDZX_REF_LIVE=1（等价 --live）——本仓 conftest/pyproject 不在
本任务写权限内，无法注册 pytest 命令行选项，故以环境变量实现同一语义；
直连被拦时设 FDZX_REF_PROXY=http://127.0.0.1:7892 走代理。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
from pathlib import Path

import pytest

from fd_zhengxin_reference import errors
from fd_zhengxin_reference import snapshot as snapshot_mod
from fd_zhengxin_reference import wikidata_politics as wpx
from fd_zhengxin_reference.config import Settings

SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "wikidata-probe-samples"
SEEDS = ["Q17427", "Q29468", "Q76"]  # 中共 / 美国共和党 / 奥巴马（探针样本同域）

# 探针样本 → 本模块查询标记（变量名同形，fixture 即响应）
DEFAULT_STUBS = {
    "person": "pol1_occupation_birth_death.json",
    "party": "party1_alignment_p1387.json",
    "event": "event3_revolution_subclass_closure.json",
    "movement": "move1_basic_inception.json",
    "anchor": "edges_P1142_party_ideology.json",
    "edge:P155": "edges_P155_follows_generic.json",
    "edge:P1365": "edges_P1365_replaces_states.json",
    "edge:P361": "edges_P361_movement_partof.json",
    "edge:P463": "edges_P463_party_to_org.json",
    "edge:P102": "pol3_party_membership_tie.json",
    "edge:P461": "domain_P461_opposite_political.json",
    "edge:P5004": "domain_P5004_opposition_political.json",
    "edge:P7047": "domain_P7047_enemy_political.json",
    "edge:P991": "edges_P991_election_winner.json",
}


def load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def merge_bindings(*parts) -> dict:
    """合并多份真实样本（文件名或已重排的 dict）的 bindings——组合桩；值全部来自
    实测样本，无一手工编造。"""
    out: dict = {"results": {"bindings": []}}
    for part in parts:
        j = load(part) if isinstance(part, str) else part
        out["results"]["bindings"].extend(j["results"]["bindings"])
    return out


def reshaped_party_rows(name: str, *sides: str) -> dict:
    """域样本（s/o 键）→ 政党查询键（party/partyLabel）；QID 与标签值原样保留。

    生产路径里这些政党本就会经 seed 邻域一跳进入实体面（P5004/P461 都是 HOP 属性），
    组合桩只是把同一批实测值摆进实体查询的响应形状。
    """
    bindings = []
    for row in load(name)["results"]["bindings"]:
        for side in sides:
            if row.get(side, {}).get("value", "").rsplit("/", 1)[-1].startswith("Q"):
                bindings.append({"party": row[side], "partyLabel": row.get(f"{side}Label")})
    return {"results": {"bindings": bindings}}


# 人物桩 = pol1 基线 + pol3 党籍关联人（真实样本合并）；政党桩 = party1 + P5004 域样本双侧
COMPOSED_STUBS = {
    "person": merge_bindings("pol1_occupation_birth_death.json", "pol3_party_membership_tie.json"),
    "party": merge_bindings(
        "party1_alignment_p1387.json",
        reshaped_party_rows("domain_P5004_opposition_political.json", "s", "o"),
    ),
}


class StubClient:
    """按查询里的 `# fdzx:<标记>` 注释回放 fixture（两遍边查询去 pass 号后同键）。"""

    def __init__(self, mapping: dict | None = None, fail=(), fail_all: bool = False,
                 error_reason: str = errors.UNREACHABLE):
        self.mapping = {**DEFAULT_STUBS, **COMPOSED_STUBS, **(mapping or {})}
        self.fail_markers = set(fail)
        self.fail_all = fail_all
        self.error_reason = error_reason
        self.calls: list[str] = []

    def query(self, sparql: str) -> dict:
        m = re.search(r"# fdzx:(\S+)", sparql)
        assert m, f"查询缺 fdzx 标记：{sparql[:80]}"
        base = re.sub(r":\d+$", "", m.group(1))  # edge:P461:2 / anchor:0 → 基础标记
        self.calls.append(base)
        if self.fail_all or base in self.fail_markers:
            raise wpx.SparqlError(self.error_reason, "stub failure")
        resp = self.mapping[base]
        return load(resp) if isinstance(resp, str) else json.loads(json.dumps(resp))


@pytest.fixture()
def conn():
    c = Settings(db_path=":memory:").connect()
    yield c
    c.close()


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    """全量离线摄取一次，模块内共享（stub 全走内存，零网络）。"""
    c = Settings(db_path=":memory:").connect()
    report = wpx.ingest_wikidata_politics(
        SEEDS, conn=c, client=StubClient(),
        snapshot_root=tmp_path_factory.mktemp("snapshots"),
    )
    assert report["status"] == "ok"
    yield c, report
    c.close()


# ---------------------------------------------------------------- 判据表


def test_polarity_table_layering() -> None:
    """极性判据表：四档分层 + 对立取信顺序 P5004 > P461(域过滤) > P7047(降权)。"""
    pol, wt = lambda pid: wpx.POLARITY_TABLE[pid][0], lambda pid: wpx.POLARITY_TABLE[pid][1]
    assert pol("P155") == pol("P1365") == "inheritance"
    assert pol("P361") == pol("P463") == pol("P102") == "alliance"
    assert {pol("P5004"), pol("P461"), pol("P7047")} == {"opposition"}
    assert wt("P5004") > wt("P461") > wt("P7047")  # 分层取信顺序
    assert pol("P991") is None and wt("P991") == 1.0  # 事件→实体关联，非极性
    # 批判档：token 合法但判据表无来源（WD 结构性缺失，P3323 全库 4 条）
    assert "critique" in wpx.POLARITY_TIERS
    assert "critique" not in {v[0] for v in wpx.POLARITY_TABLE.values()}
    assert "P3323" in wpx.CRITIQUE_GAP


def test_p39_not_a_criterion() -> None:
    """噪声收敛：P39 只作任期，不入判据表、不进查询姿势、不落任何行。"""
    assert "P39" not in wpx.TRACKED_PIDS
    assert "P39" not in wpx.HOP_PIDS
    assert "P39" not in wpx.build_person_query(SEEDS)
    # pol2 样本（P39 qualifier 含宗教人物「先知」误挂）仅作任期证据，解析面不消费


# ---------------------------------------------------------------- 查询姿势


def test_builder_postures() -> None:
    """四类实体姿势：人物 occupation 直挂；事件 subclass 闭包（直查姿势是回归项）。"""
    q = wpx.build_person_query(SEEDS, limit=7)
    assert "# fdzx:person" in q and "wdt:P106 wd:Q82955" in q and "LIMIT 7" in q
    for s in SEEDS:
        assert f"wd:{s}" in q

    q = wpx.build_event_query(SEEDS)
    # 闭包姿势：P31 取类再 P279* 上溯；直查（P31 直接 = Q40231）会漏子类（event2 vs event3 教训）
    assert "?event wdt:P31 ?class" in q and "?class wdt:P279* wd:Q40231" in q
    assert "wd:Q40231 ." not in q.replace("?class wdt:P279* wd:Q40231 .", "")  # 无直查残留

    assert "wdt:P31/wdt:P279* wd:Q7278" in wpx.build_party_query(SEEDS)
    assert "wdt:P1387 ?align" in wpx.build_party_query(SEEDS)  # 坐标锚双通道之一
    assert "wdt:P31/wdt:P279* wd:Q2738074" in wpx.build_movement_query(SEEDS)

    # 对立三属性的域处理差异：P461 查询内政治域过滤；P7047 不过滤靠降权
    q461 = wpx.build_edge_query("P461", SEEDS)
    assert "wdt:P461" in q461
    assert "wdt:P106 wd:Q82955" in q461  # 域过滤含人物
    for cls in ("Q7278", "Q2738074", "Q40231"):
        assert f"wd:{cls}" in q461  # 域过滤含政党/运动/事件类闭包
    q7047 = wpx.build_edge_query("P7047", SEEDS)
    assert "wdt:P7047" in q7047 and "wd:Q7278" not in q7047  # 无域过滤（降权取信）
    assert "wdt:P991" in wpx.build_winner_query(SEEDS) and "P585" in wpx.build_winner_query(SEEDS)
    assert "wdt:P1142" in wpx.build_anchor_query(SEEDS) and "wdt:P1387" in wpx.build_anchor_query(SEEDS)

    with pytest.raises(ValueError):
        wpx.build_edge_query("P991", SEEDS)  # P991 走 winner 专用姿势
    with pytest.raises(ValueError):
        wpx.build_edge_query("P39", SEEDS)   # 非判据属性
    with pytest.raises(ValueError):
        wpx.normalize_qids(["not-a-qid"])
    with pytest.raises(ValueError):
        wpx.normalize_qids([])
    assert wpx.normalize_qids(["Q76", "Q76", "Q17427"]) == ["Q76", "Q17427"]


def test_query_fingerprint_stable_and_seeded() -> None:
    """查询指纹：同种子同限额稳定；种子变/限额变即变（快照 version 的指纹分量）。"""
    fp1 = wpx.query_fingerprint(["Q76", "Q17427"], 200, 500)
    assert fp1 == wpx.query_fingerprint(["Q17427", "Q76"], 200, 500)  # 顺序无关
    assert fp1 != wpx.query_fingerprint(["Q76", "Q17427"], 100, 500)
    assert fp1 != wpx.query_fingerprint(["Q76"], 200, 500)
    assert re.fullmatch(r"[0-9a-f]{64}", fp1)


# ---------------------------------------------------------------- 网络层口径


class FakeOpener:
    def __init__(self, exc: Exception | None = None, payload: bytes = b"not-json"):
        self.exc = exc
        self.payload = payload
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.payload)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.status = 200

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_error_taxonomy() -> None:
    """断网/源异常口径照 errors.py：URLError/超时→unreachable；HTTP 错误/坏 JSON→upstream。"""
    cases = [
        (urllib.error.URLError(ConnectionRefusedError(61, "connection refused")), errors.UNREACHABLE),
        (urllib.error.URLError(OSError(8, "nodename nor servname provided")), errors.UNREACHABLE),
        (urllib.error.HTTPError("u", 500, "boom", {}, None), errors.UPSTREAM),
        (urllib.error.HTTPError("u", 403, "blocked", {}, None), errors.UPSTREAM),
    ]
    for exc, expected in cases:
        client = wpx.SparqlClient(opener=FakeOpener(exc=exc), retries=())
        with pytest.raises(wpx.SparqlError) as hit:
            client.query("SELECT 1 WHERE {}")
        assert hit.value.reason == expected

    client = wpx.SparqlClient(opener=FakeOpener(payload=b"<html>gateway</html>"), retries=())
    with pytest.raises(wpx.SparqlError) as hit:
        client.query("SELECT 1 WHERE {}")
    assert hit.value.reason == errors.UPSTREAM

    # 短退避 ×2：两次失败后第三次成功
    opener = FakeOpener(exc=urllib.error.URLError(TimeoutError("timed out")))
    good = wpx.SparqlClient(retries=(0.01, 0.01))
    good.opener = _FlakyOpener(opener, FakeOpener(payload=json.dumps({"results": {"bindings": []}}).encode()))
    out = good.query("SELECT 1 WHERE {}")
    assert out == {"results": {"bindings": []}}


class _FlakyOpener:
    """前两次交给 failing opener，之后交给 recovery opener（测退避重试）。"""

    def __init__(self, failing: FakeOpener, recovery: FakeOpener):
        self.failing = failing
        self.recovery = recovery
        self.seen = 0

    def open(self, req, timeout=None):
        self.seen += 1
        return (self.failing if self.seen <= 2 else self.recovery).open(req, timeout)


def test_client_proxy_from_env(monkeypatch) -> None:
    """代理走 FDZX_REF_PROXY（config 口径）：空=显式直连；设值=注入 opener。"""
    monkeypatch.delenv("FDZX_REF_PROXY", raising=False)
    assert wpx.SparqlClient().proxy == ""
    monkeypatch.setenv("FDZX_REF_PROXY", "http://127.0.0.1:7892")
    assert wpx.SparqlClient().proxy == "http://127.0.0.1:7892"
    assert wpx.SparqlClient(proxy="http://other:1").proxy == "http://other:1"


def test_client_posts_sparql_with_ua() -> None:
    """传输形态：POST 表单 + 描述性 UA + sparql-results+json Accept（探针同款习惯）。"""
    opener = FakeOpener(payload=json.dumps({"results": {"bindings": []}}).encode())
    wpx.SparqlClient(opener=opener, retries=()).query("SELECT 1 WHERE {}")

    class SpyOpener(FakeOpener):
        def open(self, req, timeout=None):
            self.last_req = req
            return super().open(req, timeout)

    spy = SpyOpener(payload=json.dumps({"results": {"bindings": []}}).encode())
    wpx.SparqlClient(opener=spy, retries=()).query("SELECT ?x WHERE {}")
    assert spy.last_req.get_header("User-agent") == wpx.UA
    assert spy.last_req.get_header("Accept") == "application/sparql-results+json"
    body = spy.last_req.data.decode()
    assert "query=SELECT+%3Fx" in body  # 表单 POST（长查询不撑请求行）


# ---------------------------------------------------------------- 摄取（离线 fixture 全链路）


def test_offline_ingest_counts_and_snapshot(ingested) -> None:
    """真实样本回放：实体/边/锚行数、快照版本化（时间+指纹）、CC0 归属、台账。"""
    conn, report = ingested
    c = report["counts"]
    # 真实样本回放的确定行数（pol1+pol3 人物 34；party1+P5004 域样本政党 54；
    # event3 事件 6；move1 运动 15；九属性边去重 115；P1387 20 + P1142 8 锚）
    assert c["entities"] == 109
    assert c["by_kind"] == {"politician": 34, "party": 54, "event": 6, "movement": 15}
    assert c["relations"] == 115
    assert c["by_polarity"] == {"inheritance": 18, "alliance": 36, "opposition": 53, "none": 8}
    assert c["anchors"] == 28
    assert c["queries_ok"] == 23  # 4 实体 + 1 锚批 + 9 属性 × 2 遍（seed 一跳 + 二跳）
    assert c["queries_failed"] == 0 and report["failures"] == []
    assert report["reason"] is None and report["seeds"] == SEEDS

    # 快照版本化：WD 无版本号 → 摄取时间 + 查询指纹
    assert re.fullmatch(r"\d{8}T\d{6}Z\+fp[0-9a-f]{12}", report["snapshot_version"])
    manifest = snapshot_mod.latest(conn, wpx.DATASET)
    assert manifest["snapshot_id"] == report["snapshot_id"]
    assert "CC0" in manifest["attribution"]
    payload = Path(manifest["storage_path"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == manifest["sha256"]
    assert set(json.loads(payload)) >= {"person", "party", "event", "movement", "edge:P461:1"}

    # 台账
    row = conn.execute(
        "SELECT status, detail FROM ingest_log WHERE source='wikidata'"
        " ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "ok" and "snapshot_id" in row["detail"]


def test_offline_ingest_rows_traceable(ingested) -> None:
    """可回溯形态：每边带 PID/极性/权重/标签；每实体/边/锚挂快照 id。"""
    conn, report = ingested
    sid = report["snapshot_id"]

    edge = conn.execute(
        "SELECT * FROM wd_political_relations WHERE source_id='Q1717759' AND property_pid='P155'"
    ).fetchone()
    assert edge["target_id"] == "Q1284104" and edge["polarity"] == "inheritance"
    assert edge["weight"] == 1.0 and edge["kind"] == "follows"
    assert edge["snapshot_version"] == sid  # 探针 edges_P155 的选举届次链示例

    for pid, pol, wt in [("P5004", "opposition", 1.0), ("P461", "opposition", 0.8),
                         ("P7047", "opposition", 0.4)]:
        row = conn.execute(
            "SELECT polarity, weight FROM wd_political_relations WHERE property_pid=?", (pid,)
        ).fetchone()
        assert (row["polarity"], row["weight"]) == (pol, wt)

    winner = conn.execute(
        "SELECT * FROM wd_political_relations WHERE property_pid='P991' LIMIT 1"
    ).fetchone()
    assert winner["polarity"] is None and winner["kind"] == "winner"

    assert conn.execute(
        "SELECT COUNT(*) FROM wd_political_relations WHERE snapshot_version != ?", (sid,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM wd_political_relations WHERE property_pid='P39'"
    ).fetchone()[0] == 0  # 任期属性不产边（噪声收敛）

    # label service 回填 QID 串的坑：party1 的 Q3055016 无标签 → NULL 不硬造
    row = conn.execute(
        "SELECT label_en FROM wd_political_entities WHERE qid='Q3055016'"
    ).fetchone()
    assert row is not None and row["label_en"] is None


# ---------------------------------------------------------------- 查询面


def test_query_entity(ingested) -> None:
    conn, report = ingested
    out = wpx.query_entity(conn, "Q4715")  # pol1：Moncef Marzouki
    assert len(out["results"]) == 1
    ent = out["results"][0]
    assert ent["kind"] == "politician" and ent["label_en"] == "Moncef Marzouki"
    assert ent["provenance"]["source"] == "wikidata"
    assert ent["provenance"]["snapshot_version"] == report["snapshot_id"]
    assert "CC0" in ent["provenance"]["attribution"]

    miss = wpx.query_entity(conn, "Q999999999")
    assert miss["results"] == [] and miss["reason"] == errors.NOT_INGESTED and miss["hint"]
    with pytest.raises(ValueError):
        wpx.query_entity(conn, "junk")


def test_query_relations_polarity_filter(ingested) -> None:
    """极性过滤 + 双向边：Q29468（美国共和党，pol3/domain 样本交汇点）。"""
    conn, _ = ingested
    all_rows = wpx.query_relations(conn, "Q29468")["results"]
    assert len(all_rows) == 23  # 20×P102(入) + 2×P461(双向) + 1×P5004(入)

    ally = wpx.query_relations(conn, "Q29468", polarity="alliance")["results"]
    assert len(ally) == 20 and {r["property_pid"] for r in ally} == {"P102"}
    assert all(r["direction"] == "in" and r["polarity"] == "alliance" for r in ally)

    oppo = wpx.query_relations(conn, "Q29468", polarity="opposition")["results"]
    assert {r["property_pid"] for r in oppo} == {"P461", "P5004"}
    assert oppo[0]["weight"] >= oppo[-1]["weight"]  # 权重降序（分层取信序）

    inherit = wpx.query_relations(conn, "Q29468", polarity="inheritance")
    assert inherit["results"] == [] and inherit["reason"] == errors.NOT_INGESTED
    assert "inheritance" in inherit["hint"]

    missing = wpx.query_relations(conn, "Q999999999")
    assert missing["results"] == [] and missing["reason"] == errors.NOT_INGESTED
    with pytest.raises(ValueError):
        wpx.query_relations(conn, "Q29468", polarity="bogus")


def test_query_relations_critique_gap(ingested) -> None:
    """批判档：恒空 + not_ingested + 缺口说明（P3323 全库 4 条，其他源承载）。"""
    conn, _ = ingested
    out = wpx.query_relations(conn, "Q29468", polarity="critique")
    assert out["results"] == [] and out["reason"] == errors.NOT_INGESTED
    assert "P3323" in out["hint"]


def test_query_ideology_anchor(ingested) -> None:
    """坐标锚双通道：edges_P1142 的 P1142（含实体 QID）+ party1 的 P1387（连续倾向）。"""
    conn, _ = ingested
    out = wpx.query_ideology_anchor(conn, "Q3173")  # Green Alliance–The People's Party
    anchors = {(r["anchor_pid"], r["anchor_label"], r["anchor_qid"]) for r in out["results"]}
    assert ("P1142", "social democracy", "Q121254") in anchors
    assert ("P1142", "green politics", "Q1661415") in anchors
    assert all(r["provenance"]["snapshot_version"] for r in out["results"])

    p1387 = wpx.query_ideology_anchor(conn, "Q2914027")["results"]  # party1：right-wing
    assert [(r["anchor_pid"], r["anchor_label"]) for r in p1387] == [("P1387", "right-wing")]

    miss = wpx.query_ideology_anchor(conn, "Q4715")  # 人物面无锚（样本实证）
    assert miss["results"] == [] and miss["reason"] == errors.NOT_INGESTED
    assert "P1142" in miss["hint"] and "P1387" in miss["hint"]


# ---------------------------------------------------------------- 断网口径


def test_ingest_all_queries_unreachable(conn, tmp_path) -> None:
    """断网全灭：status=failed + reason=unreachable（taxonomy），不落库不编造。"""
    report = wpx.ingest_wikidata_politics(
        SEEDS, conn=conn, client=StubClient(fail_all=True, error_reason=errors.UNREACHABLE),
        snapshot_root=tmp_path,
    )
    assert report["status"] == "failed"
    assert report["reason"] == errors.UNREACHABLE  # 口径在 taxonomy 内
    assert report["snapshot_id"] is None and report["counts"] == {}
    assert all(f["reason"] == errors.UNREACHABLE for f in report["failures"])
    log = conn.execute(
        "SELECT status FROM ingest_log WHERE source='wikidata' ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    assert log["status"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM wd_political_entities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM wd_political_relations").fetchone()[0] == 0


def test_ingest_upstream_partial(conn, tmp_path) -> None:
    """部分失败：status=partial，其余照常入库；单源 5xx 记 upstream 口径。"""
    report = wpx.ingest_wikidata_politics(
        SEEDS, conn=conn,
        client=StubClient(fail=["person", "edge:P7047"], error_reason=errors.UPSTREAM),
        snapshot_root=tmp_path,
    )
    assert report["status"] == "partial" and report["reason"] == errors.UPSTREAM
    assert {f["query"].rsplit(":", 1)[0] for f in report["failures"]} == {"person", "edge:P7047"}
    c = report["counts"]
    assert c["by_kind"]["politician"] == 0 and c["by_kind"]["party"] == 54
    assert c["relations"] == 95  # 115 - 20 条 P7047
    log = conn.execute(
        "SELECT status FROM ingest_log WHERE source='wikidata' ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    assert log["status"] == "partial"


def test_reingest_full_refresh(conn, tmp_path) -> None:
    """全量刷新语义：二次摄取只保新快照行（旧政党实体清出），快照清单两份皆在。"""
    first = wpx.ingest_wikidata_politics(
        SEEDS, conn=conn, client=StubClient(), snapshot_root=tmp_path)
    assert first["status"] == "ok"
    second = wpx.ingest_wikidata_politics(
        SEEDS, conn=conn,
        client=StubClient(fail=["party", "event", "movement", "anchor"]),
        snapshot_root=tmp_path,
    )
    assert second["status"] == "partial"
    assert second["counts"]["by_kind"] == {"politician": 34, "party": 0, "event": 0, "movement": 0}
    # 旧快照的政党行已清出；当前快照只剩人物
    assert wpx.query_entity(conn, "Q2914027")["reason"] == errors.NOT_INGESTED
    assert wpx.query_entity(conn, "Q4715")["results"][0]["provenance"]["snapshot_version"] \
        == second["snapshot_id"]
    assert wpx.query_ideology_anchor(conn, "Q3173")["reason"] == errors.NOT_INGESTED
    manifests = snapshot_mod.list_for(conn, wpx.DATASET)
    assert len(manifests) == 2  # 快照清单保留历史（不可变），wd_* 表只映当前


# ---------------------------------------------------------------- 解析容错（样本全扫）


def test_parsers_tolerate_every_probe_sample() -> None:
    """36 份样本逐一过解析面：姿势匹配的直接解析，侦察/台账类只验证不炸。"""
    files = sorted(p.name for p in SAMPLES.glob("*.json"))
    assert len(files) >= 36  # 探针定案 36 份；目录现有 38 份（后续补充亦不缩水）
    for name in files:
        j = load(name)
        assert isinstance(j.get("results", {}).get("bindings", []), list)

    # 各姿势的补充样本（非 stub 主选）也能解析；多行同 QID 按实体合并（首见非空占位）
    assert len(wpx.parse_event_rows(load("event2_revolution_direct.json"))) == 18
    assert len(wpx.parse_event_rows(load("event4_coup_with_time.json"))) == 20
    assert len(wpx.parse_event_rows(load("event1_election_with_winner.json"))) == 17
    assert len(wpx.parse_party_rows(load("party2_ideology_p1142.json"))[0]) == 3  # 中共三行意识形态
    assert len(wpx.parse_party_rows(load("party3_china_zh.json"))[0]) == 8
    assert len(wpx.parse_movement_rows(load("move2_ideology_closure.json"))) == 6
    assert len(wpx.parse_movement_rows(load("move3_follows_partof.json"))) == 18

    # 域侦察样本（classLabel-only，无 o 端点）：解析归零不炸
    assert wpx.parse_edge_rows(load("domain_P7047_enemy_whatsubject.json"), "P7047") == []
    assert len(wpx.parse_edge_rows(load("domain_P5004_opposition_sample.json"), "P5004")) > 0

    # zh 标签通道：partyLabelZh 存在时入 label_zh（party3 实测样本该列全空，合成行验证通道）
    synthetic = {"results": {"bindings": [{
        "party": {"type": "uri", "value": "http://www.wikidata.org/entity/Q17427"},
        "partyLabelZh": {"xml:lang": "zh", "type": "literal", "value": "中国共产党"},
    }]}}
    ents, _ = wpx.parse_party_rows(synthetic)
    assert ents[0].label_zh == "中国共产党"

    # 批判档缺口证据：usage 样本实测 P3323 全库 4 条
    n = load("usage_P3323_disputation.json")["results"]["bindings"][0]["n"]["value"]
    assert int(n) == 4


# ---------------------------------------------------------------- live 冒烟（默认跳过）


def _live_enabled() -> bool:
    return os.environ.get("FDZX_REF_LIVE", "").strip().lower() not in ("", "0", "false", "no")


@pytest.mark.live
@pytest.mark.skipif(
    not _live_enabled(),
    reason="live 冒烟默认跳过：FDZX_REF_LIVE=1 时真打一次 WDQS 小 LIMIT 查询"
           "（--live 门控以环境变量实现：conftest/pyproject 不在本任务写权限内）；"
           "直连被拦时另设 FDZX_REF_PROXY=http://127.0.0.1:7892",
)
def test_live_smoke_small_sparql_query() -> None:
    """真打一次：occupation 判据小查询（种子 Q76 奥巴马，LIMIT 5）。"""
    client = wpx.SparqlClient(retries=())
    j = client.query(wpx.build_person_query(["Q76"], limit=5))
    rows = j.get("results", {}).get("bindings", [])
    assert rows, "live 查询空结果"
    qids = {r["person"]["value"].rsplit("/", 1)[-1] for r in rows}
    assert "Q76" in qids  # seed 自身命中 occupation 判据分支
