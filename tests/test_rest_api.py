"""只读 REST 锚点面（D3）：regions 坐标系 + concepts/{topic}/anchor 前提锚。

主服务个人层消费面（fd-zhengxin 的 reference_client.py）的契约测试：形态
（regions 裸列表键 + name 条目 / key_points 字符串条目）、来源（库内 curated
层，断网可查——SEP 断网不影响）、空结果失败口径（reason + source_status 分项）。
"""

from __future__ import annotations

import pytest

from fd_zhengxin_reference import sep_politics
from fd_zhengxin_reference.config import Settings
from fd_zhengxin_reference.server import build_http_app
from fd_zhengxin_reference.sep_politics import SepUnreachable

from starlette.testclient import TestClient


async def _sep_offline(*a, **kw):
    raise SepUnreachable("offline (rest api test)")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FDZX_REF_DB", str(tmp_path / "ref.db"))
    monkeypatch.setenv("FDZX_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return Settings.from_env()


@pytest.fixture()
def app(env, monkeypatch):
    """断网形态（SEP 不可达）：REST 面只读库内 curated 层，照常出数。"""
    monkeypatch.setattr(sep_politics, "fetch_search_html", _sep_offline)
    return build_http_app(env)


@pytest.fixture()
def client(app):
    # lifespan 上下文：FastMCP streamable-http 的会话管理器要随 app 生命周期启动
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- /healthz 与挂载

def test_healthz_still_up(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["service"] == "fd-zhengxin-reference"


def test_mcp_face_untouched(client) -> None:
    """/mcp 面不受 REST 追加影响：无会话握手直发 tools/call 仍 400（原有纪律）。"""
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "concept_lookup", "arguments": {"term": "仁政"}}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------- /api/v1/regions

def test_regions_seed_schools_plus_modern_currents(client) -> None:
    r = client.get("/api/v1/regions")
    assert r.status_code == 200
    body = r.json()
    names = [item["name"] for item in body["regions"]]
    # 先秦八学派 + 近现代五思潮 = 13 个坐标系区域
    assert len(names) == 13
    assert {"儒家", "道家", "法家", "墨家", "名家", "阴阳家", "兵家", "纵横家"} <= set(names)
    assert {"维新变法思潮", "自由主义思潮", "社会主义论战"} <= set(names)
    # 宽容解析形态：条目带 name；source_status 分项记录 curated 两源状态
    assert all(item.get("name") for item in body["regions"])
    assert body["source_status"]["china-seed"]["status"] == "ok"
    assert body["source_status"]["china-seed"]["snapshot"].startswith("china-seed@")
    assert body["source_status"]["modern-index"]["status"] == "ok"


def test_regions_empty_db_failure_kou_jing(tmp_path) -> None:
    """curated 层未加载（空库直读，不经启动钩子）：空 regions + reason +
    source_status 如实（走 list_regions 纯查询面——build_http_app 的启动钩子
    会物化 curated 层，造不出未加载态）。"""
    from fd_zhengxin_reference import rest_api, schema

    settings = Settings(db_path=str(tmp_path / "empty.db"))
    conn = settings.connect()
    schema.init(conn)  # 只建表，不加载 curated 层
    conn.close()
    body = rest_api.list_regions(settings)
    assert body["regions"] == []
    assert body["reason"] == "not_ingested"
    assert body["source_status"]["china-seed"]["status"] == "not_ingested"


# ---------------------------------------------------------------- /api/v1/concepts/{topic}/anchor

def test_anchor_seed_concept_split_premises(client) -> None:
    """种子概念：key_points = 一句话界定 + 原典篇目锚（snippet 按 ｜ 切分）。"""
    r = client.get("/api/v1/concepts/仁政/anchor")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "china-seed"
    assert body["provenance"]["stable_id"] == "china-seed:concept:仁政"
    assert len(body["key_points"]) == 2
    assert "不忍人之政" in body["key_points"][0]
    assert "原典" in body["key_points"][1] and "梁惠王上" in body["key_points"][1]


def test_anchor_modern_current_summary(client) -> None:
    """近现代思潮：key_points = [summary]，stable_id 为思潮 current_id。"""
    r = client.get("/api/v1/concepts/自由主义思潮/anchor")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "modern-index"
    assert body["provenance"]["stable_id"] == "modern:china/current/liberalism"
    assert len(body["key_points"]) == 1 and "自由主义" in body["key_points"][0]


def test_anchor_substring_match_on_free_topic(client) -> None:
    """卡面自由文本（主服务学习卡 topic 原样传入）：最长子串命中种子标签。"""
    r = client.get("/api/v1/concepts/孟子的仁政思想/anchor")
    body = r.json()
    assert body["term"] == "仁政"  # 「仁」（1 字）不抢「仁政」（2 字）的最长子串
    assert body["source"] == "china-seed"


def test_anchor_modern_substring_match(client) -> None:
    body = client.get("/api/v1/concepts/自由主义思潮的人权论战/anchor").json()
    assert body["source"] == "modern-index" and body["term"] == "自由主义思潮"


def test_anchor_miss_honest_empty(client) -> None:
    """未命中：key_points 空 + reason + hint（不猜前提），HTTP 仍 200（区别于不可达）。"""
    r = client.get("/api/v1/concepts/量子宪政主义/anchor")
    assert r.status_code == 200
    body = r.json()
    assert body["key_points"] == []
    assert body["reason"] == "not_ingested"
    assert "不猜测" in body["hint"]


def test_anchor_url_encoded_chinese_topic(client) -> None:
    """中文 topic 经 URL 编码（主服务 quote(topic) 形态）同路命中。"""
    from urllib.parse import quote

    r = client.get(f"/api/v1/concepts/{quote('自由主义思潮')}/anchor")
    assert r.status_code == 200
    assert r.json()["source"] == "modern-index"
