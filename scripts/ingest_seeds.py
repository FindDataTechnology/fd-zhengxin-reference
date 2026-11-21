#!/usr/bin/env python3
"""种子清单摄取的唯一入口：一条命令跑完「摄取 → 导出 → 打印入库命令」。

存在理由：2026-11-22 那次补摄取的四步（Mac 摄取 → 导出 delta 表 →
kubectl cp → pod 内 ATTACH 合并）是手敲的，参数与顺序散在会话记录里。
下次补实体的人不该重推一遍四个坑（见记忆 `wikidata-ingest-four-traps`）。

两个障碍写在这里，免得再踩：
1. **参照系 pod 出不了网**（`query.wikidata.org` SSL handshake timeout），
   所以摄取必须在有出口的机器上跑（Mac 直连实测 2s）。
2. **`kubectl cp` 传 600MB 会截断**（实测 598MB → 11MB），所以不能换整库，
   只能导 `wd_*` 三表 + 元数据行（约 380KB）再在 pod 内 ATTACH 合并。

用法：

    # 1) 摄取 + 导出 delta（在有网机器上）
    python3 scripts/ingest_seeds.py ingest --qids Q15031 Q16977 \\
        --db /tmp/zx.db --delta /tmp/wd_delta.sqlite

    # 2) 按它打印的命令把 delta 送进 pod 并合并
    #    （脚本不直接动集群——那是运维动作，不该藏在代码里）

退出码：0 成功；2 摄取或导出失败。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_zhengxin_reference import wikidata_politics as wpx  # noqa: E402
from fd_zhengxin_reference.config import Settings  # noqa: E402

DELTA_TABLES = ("wd_political_entities", "wd_political_relations",
                "wd_ideology_anchors")
# 元数据行按来源过滤：只要 wikidata 的，不碰 v-dem/marpor/china-seed 的行
META_FILTERS = {
    "snapshots": "dataset='wikidata-politics'",
    "ingest_log": "source='wikidata'",
}


def ingest(qids: list[str], db_path: str, *, limit: int, edge_limit: int) -> dict:
    """跑摄取（需要外网）。返回报告。"""
    os.environ["FDZX_REF_DB"] = db_path
    return wpx.ingest_wikidata_politics(
        qids, limit=limit, edge_limit=edge_limit, expand=True,
    )


def export_delta(src_path: str, out_path: str) -> dict:
    """导 delta 库：三张 wd 表全量 + 元数据表的 wikidata 行 + 相关索引。

    不导 concepts/currents/relations/regime_indicators——那些是别的源，
    在 pod 内 ATTACH 合并时不该被覆盖。
    """
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(out_path)
    counts: dict[str, int] = {}
    try:
        for t in DELTA_TABLES:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not ddl:
                raise RuntimeError(f"源库缺表 {t}")
            dst.execute(ddl[0])
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            cols = [d[0] for d in src.execute(f"SELECT * FROM {t} LIMIT 0").description]
            dst.executemany(
                f"INSERT INTO {t} VALUES ({','.join('?' * len(cols))})",
                [tuple(r) for r in rows],
            )
            counts[t] = len(rows)
        for t, where in META_FILTERS.items():
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if not ddl:
                continue
            dst.execute(ddl[0])
            rows = src.execute(f"SELECT * FROM {t} WHERE {where}").fetchall()
            cols = [d[0] for d in src.execute(f"SELECT * FROM {t} LIMIT 0").description]
            dst.executemany(
                f"INSERT INTO {t} VALUES ({','.join('?' * len(cols))})",
                [tuple(r) for r in rows],
            )
            counts[t] = len(rows)
        # 索引：合并不重建索引会慢；sqlite_master 里 sql 非空的才是可重建的
        for idx in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            f" AND tbl_name IN ({','.join('?' * len(DELTA_TABLES))})",
            DELTA_TABLES,
        ):
            try:
                dst.execute(idx[0])
            except sqlite3.OperationalError:
                pass   # 索引已随 DDL 建过
        dst.commit()
    finally:
        dst.close()
        src.close()
    return counts


def _pod_merge_command(delta_path: str) -> str:
    """打印送进 pod 并合并的完整命令（含备份与回滚提示）。"""
    db = Settings.from_env().db_path
    return f"""# 下面这些是运维动作，脚本不代跑。逐条执行：

POD=$(kubectl get pods -n fd-prod -l app.kubernetes.io/name=fd-zhengxin-reference \\
      --field-selector=status.phase=Running -o jsonpath='{{.items[0].metadata.name}}')

# 1) 备份（sqlite 在线备份，比 cp 安全）
kubectl exec -n fd-prod "$POD" -- python3 -c "
import sqlite3
s=sqlite3.connect('file:{db}?mode=ro',uri=True)
d=sqlite3.connect('{db}.bak-$(date +%Y%m%d)')
s.backup(d); d.close(); s.close()"

# 2) 送 delta（约 380KB；整库 600MB 会截断，别试）
kubectl cp {delta_path} fd-prod/$POD:/tmp/wd_delta.sqlite

# 3) ATTACH 合并（三张 wd 表全量刷新；元数据 upsert）
kubectl exec -n fd-prod "$POD" -- python3 -c "
import sqlite3
c=sqlite3.connect('{db}')
c.execute(\\"ATTACH '/tmp/wd_delta.sqlite' AS d\\")
c.execute('BEGIN')
try:
    for t in {list(DELTA_TABLES)!r}:
        c.execute(f'DELETE FROM {{t}}')
        c.execute(f'INSERT INTO {{t}} SELECT * FROM d.{{t}}')
    c.execute('INSERT OR REPLACE INTO snapshots SELECT * FROM d.snapshots')
    c.execute('INSERT OR REPLACE INTO ingest_log SELECT * FROM d.ingest_log')
    c.commit()
except Exception:
    c.rollback(); raise
c.execute('DETACH d'); c.close()
print('merged')"

# 4) 验收：中文/英文/繁简各查一个
kubectl exec -n fd-prod "$POD" -- python3 -c "
import asyncio
from fd_zhengxin_reference.server import create_server
async def m():
    fn=next(t.fn for t in await create_server().list_tools() if t.name=='entity_dossier')
    for r in ['习近平','Xi Jinping','邓小平']:
        o=await fn(ref=r)
        print(r, (o.get('coverage') or {{}}).get('status'), (o.get('identity') or {{}}).get('label'))
asyncio.run(m())"

# 回滚：mv {db}.bak-<日期> {db}
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wikidata 种子摄取 + delta 导出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="摄取种子并导出 delta（需要外网）")
    ing.add_argument("--qids", nargs="+", required=True, help="种子 QID 列表")
    ing.add_argument("--db", required=True, help="摄取产物库路径（临时即可）")
    ing.add_argument("--delta", required=True, help="delta 输出路径")
    ing.add_argument("--limit", type=int, default=200)
    ing.add_argument("--edge-limit", type=int, default=500)

    ap_p = sub.add_parser("export", help="只从已有库导 delta（不需要外网）")
    ap_p.add_argument("--db", required=True)
    ap_p.add_argument("--delta", required=True)

    args = ap.parse_args(argv)

    try:
        if args.cmd == "ingest":
            rep = ingest(args.qids, args.db, limit=args.limit, edge_limit=args.edge_limit)
            if rep.get("status") not in ("ok", "partial"):
                print(f"摄取失败：{rep.get('status')} / {rep.get('reason')}", file=sys.stderr)
                return 2
            print(f"摄取 {rep['status']}：{rep['counts']}")
        counts = export_delta(args.db, args.delta)
    except Exception as exc:  # noqa: BLE001 —— 失败与「结果为空」不可混
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    size = Path(args.delta).stat().st_size // 1024
    print(f"delta 写出 {args.delta}（{size} KB）")
    for t, n in counts.items():
        print(f"  {t}: {n}")
    print()
    print(_pod_merge_command(args.delta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
