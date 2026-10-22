"""参照系库 DDL（SQLite，可重建缓存；照 fd-zhesi-reference 的库形制）。

中西无感：概念/思潮/关系/指标均为开放词表（不绑任何地域枚举）；坐标轴集与
指标集随源落定，schema 只存 JSON 坐标与开放指标名。骨架期建好即用（空表 →
占位工具报 not_ingested），接线波次按需补增量列。
"""

from __future__ import annotations

import sqlite3

DDL = [
    # 概念引用层缓存（SEP + 中国线词条；只存标题+短引用+链接，不存正文）
    """CREATE TABLE IF NOT EXISTS concepts (
        term TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        snippet TEXT,
        retrieved_at TEXT NOT NULL,
        PRIMARY KEY (term, url)
    )""",
    # 思潮坐标层（MARPOR/中国线：思潮/思想家/政党家族的坐标）
    """CREATE TABLE IF NOT EXISTS currents (
        current_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        label_en TEXT,
        label_zh TEXT,
        kind TEXT,               -- current 思潮 / thinker 思想家 / party 政党家族（开放词表）
        coordinates TEXT,        -- JSON：轴名 -> 值（轴集随源落定，中西无感）
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT
    )""",
    # 关系层（思想传承/批判/论战边；极性口径对齐 zhesi 线）
    """CREATE TABLE IF NOT EXISTS relations (
        relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        kind TEXT NOT NULL,      -- influence 影响 / critique 批判 / debate 论战（开放词表）
        polarity TEXT,           -- positive / critical / synthetic
        weight REAL,
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT
    )""",
    # 政体时序层（国家×年×指标；V-Dem 指数 / MARPOR 立场指标）
    """CREATE TABLE IF NOT EXISTS regime_indicators (
        country_code TEXT NOT NULL,
        year INTEGER NOT NULL,
        indicator TEXT NOT NULL,
        value REAL,
        ingested_at TEXT NOT NULL,
        snapshot_version TEXT,
        PRIMARY KEY (country_code, year, indicator)
    )""",
    # 摄取台账（source_status 的数据面）
    """CREATE TABLE IF NOT EXISTS ingest_log (
        source TEXT NOT NULL,
        status TEXT NOT NULL,     -- ok / partial / failed / not_ingested
        detail TEXT,
        as_of TEXT NOT NULL,
        PRIMARY KEY (source, as_of)
    )""",
    # 来源状态表：可得性探测结果，与许可判定分开记录（后续波次的探测写入）
    """CREATE TABLE IF NOT EXISTS source_status (
        source TEXT NOT NULL,
        checked_at TEXT NOT NULL,
        status TEXT NOT NULL,       -- available / unavailable
        reason TEXT,                -- unreachable / timeout / http_error / not_probed
        url TEXT,
        http_status INTEGER,
        elapsed_ms INTEGER,
        content_sha256 TEXT,
        content_bytes INTEGER,
        detail TEXT,
        PRIMARY KEY (source, checked_at)
    )""",
    # 快照清单：静态数据集版本化的唯一摄取入口
    """CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id TEXT PRIMARY KEY,   -- <dataset>@<version>#<sha256 前 12>
        dataset TEXT NOT NULL,
        version TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source_url TEXT,
        sha256 TEXT NOT NULL,
        attribution TEXT,
        probe_status TEXT NOT NULL,     -- available / unavailable
        storage_path TEXT NOT NULL,
        bytes INTEGER,
        created_at TEXT NOT NULL
    )""",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_currents_labels ON currents(label_en, label_zh)",
    "CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_regime_country_year ON regime_indicators(country_code, year)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_dataset ON snapshots(dataset, fetched_at)",
    "CREATE INDEX IF NOT EXISTS idx_source_status_source ON source_status(source, checked_at)",
]


def init(conn: sqlite3.Connection) -> None:
    for ddl in DDL:
        conn.execute(ddl)
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()
