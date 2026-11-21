#!/usr/bin/env python3
"""实体覆盖率探针：量「工具存在」与「查得到」之间的距离。

存在理由（change zhengxin-entity-and-impact-loops task 1.5 / design D2）：
`query_relations` 对不在快照种子邻域内的 QID 一律 not_ingested。不先量就会做出
「工具上线了但大多数人查不到」，而所有检查都是绿的（同族教训见 workspace 的
`ci-gate-never-ran-looks-green` / `gap-declared-but-never-emitted`）。

**判据纪律**：三桶分别计数并各自可解释，不以单个退出码表达结论。
`not_ingested`（无快照）与 `not_in_snapshot`（快照在但无此实体）必须分开——
两者修复方向不同（前者跑摄取，后者补该实体的种子邻域）。

用法：

    # 量种子 40 人物（curated 面，不需要 wikidata 快照）
    python3 scripts/coverage_report.py

    # 追加一批 QID（近现代/当代实体）
    python3 scripts/coverage_report.py --qids Q15031 Q16977

    # 机器可读
    python3 scripts/coverage_report.py --qids Q15031 --json

退出码：0 = 测量完成（无论覆盖率高低）；2 = 测量本身失败（库打不开等）。
覆盖率**低不是失败**——探针的职责是让数字可见，不是让数字好看。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_zhengxin_reference import china_lineage, schema, wikidata_politics as wpx  # noqa: E402
from fd_zhengxin_reference.config import Settings  # noqa: E402
from fd_zhengxin_reference.entity_dossier import (  # noqa: E402
    COVERAGE_AVAILABLE,
    COVERAGE_NOT_IN_SNAPSHOT,
    COVERAGE_NOT_INGESTED,
    _resolve,
    _seed_snapshot_id,
    _wd_snapshot_id,
)

BUCKETS = (COVERAGE_AVAILABLE, COVERAGE_NOT_IN_SNAPSHOT, COVERAGE_NOT_INGESTED)


def measure(refs: list[str], settings: Settings) -> dict:
    conn = settings.connect()
    try:
        schema.init(conn)
        wpx.ensure_tables(conn)
        # curated 层由服务启动钩子加载；探针常跑在裸库上，必须自己装一次，
        # 否则种子人物会全部「无法解析」——那种失败是探针的缺陷，不是覆盖率的真相。
        from fd_zhengxin_reference.server import bootstrap_curated

        bootstrap_curated(settings)
        wd_snap = _wd_snapshot_id(conn)
        seed_snap = _seed_snapshot_id(conn)
        rows = []
        for ref in refs:
            resolution, hit = _resolve(conn, ref)
            if resolution["status"] != "found":
                # 解析不出来的实体无法判归属快照——单独成桶，不与三态混算
                bucket = "unresolved"
                detail = resolution.get("reason") or resolution["status"]
            elif hit["face"] == "wikidata":
                if wd_snap is None:
                    bucket = COVERAGE_NOT_INGESTED
                    detail = "wikidata 面无快照"
                else:
                    exists = conn.execute(
                        "SELECT 1 FROM wd_political_entities WHERE qid=? AND snapshot_version=?",
                        (hit["qid"], wd_snap),
                    ).fetchone()
                    bucket = COVERAGE_AVAILABLE if exists else COVERAGE_NOT_IN_SNAPSHOT
                    detail = f"snapshot={wd_snap}"
            else:
                bucket = COVERAGE_AVAILABLE
                detail = f"snapshot={seed_snap}"
            rows.append({"ref": ref, "bucket": bucket, "detail": detail,
                         "face": hit["face"] if hit else None})
    finally:
        conn.close()

    counts = {b: 0 for b in BUCKETS}
    counts["unresolved"] = 0
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

    return {
        "total": len(rows),
        "counts": counts,
        "snapshots": {"wikidata-politics": wd_snap, "china-seed": seed_snap},
        "rows": rows,
        "remediation": _remediation(counts),
    }


def _remediation(counts: dict) -> list[str]:
    """按桶给出修复方向——两种「查不到」的修法不同，不能合并成一句。"""
    out = []
    if counts.get(COVERAGE_NOT_INGESTED):
        out.append(
            f"{counts[COVERAGE_NOT_INGESTED]} 个实体无快照：跑摄取（参照系 pod 到 wikidata 不可达，"
            "须在集群外用 --ingest 跑完再同步 reference.db，见 reports/baseline-2026-11-21.md）"
        )
    if counts.get(COVERAGE_NOT_IN_SNAPSHOT):
        out.append(
            f"{counts[COVERAGE_NOT_IN_SNAPSHOT]} 个实体不在快照：把它们加进摄取种子邻域后重跑"
        )
    if counts.get("unresolved"):
        out.append(
            f"{counts['unresolved']} 个引用无法解析：检查标签是否精确匹配（curated 面用全名）"
        )
    if not out:
        out.append("全部实体可用")
    return out


def seed_refs() -> list[str]:
    """种子 40 人物的标签（curated 面，无需 wikidata 快照即可量）。"""
    return [
        f["label"]
        for f in china_lineage.load_seed_data().get("figures") or []
        if f.get("label")
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="参照系实体覆盖率探针")
    ap.add_argument("--qids", nargs="*", default=[], help="追加测量的实体引用（QID 或标签）")
    ap.add_argument("--no-seed", action="store_true", help="不测量种子人物")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    refs = ([] if args.no_seed else seed_refs()) + list(args.qids)
    if not refs:
        print("没有要测量的实体（用 --qids 指定，或不加 --no-seed）", file=sys.stderr)
        return 2

    try:
        settings = Settings.from_env()
        report = measure(refs, settings)
    except Exception as exc:  # noqa: BLE001 —— 测量失败 ≠ 覆盖率低，两者不可混
        print(f"测量失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    c = report["counts"]
    print(f"实体覆盖率（{report['total']} 个实体）")
    print(f"  可用          {c[COVERAGE_AVAILABLE]:>4}")
    print(f"  在快照外      {c[COVERAGE_NOT_IN_SNAPSHOT]:>4}")
    print(f"  未摄取        {c[COVERAGE_NOT_INGESTED]:>4}")
    print(f"  无法解析      {c['unresolved']:>4}")
    print(f"  快照：wikidata={report['snapshots']['wikidata-politics']} "
          f"china-seed={report['snapshots']['china-seed']}")
    print()
    print("修复方向：")
    for line in report["remediation"]:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
