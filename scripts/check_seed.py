#!/usr/bin/env python3
"""种子 JSON 结构自检（CI 可复用）：粒度区间、枚举、双锚、来源与 QID 纪律。

用法：python3 scripts/check_seed.py [--path seed/china_seed.json]
退出码：0 通过；1 有违例。只读，不写任何文件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_zhengxin_reference.china_lineage import (  # noqa: E402
    default_seed_path,
    load_seed_data,
    validate_seed,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="先秦诸子种子谱系 JSON 自检")
    parser.add_argument("--path", type=Path, default=None, help="种子 JSON 路径（默认 seed/china_seed.json）")
    args = parser.parse_args()

    path = args.path or default_seed_path()
    data = load_seed_data(path)
    violations = validate_seed(data)

    meta = data.get("meta") or {}
    gran = meta.get("granularity") or {}
    counts = {k: len(data.get(k) or []) for k in ("schools", "figures", "concepts", "edges")}
    print(f"种子：{path}")
    print(f"版本：{meta.get('version')}（curated_at={meta.get('curated_at')}）")
    for key in ("schools", "figures", "concepts", "edges"):
        lo, hi = gran.get(key, ("?", "?"))
        ok = "OK" if (isinstance(lo, int) and lo <= counts[key] <= hi) else "越界"
        print(f"  {key:<9} {counts[key]:>4}  （定案区间 {lo}–{hi}）{ok}")

    polarity = {e["polarity"] for e in data.get("edges") or []}
    qid_filled = sum(
        1
        for section in ("schools", "figures", "concepts")
        for item in data.get(section) or []
        if item.get("qid")
    )
    print(f"  边极性分布：{ {p: sum(1 for e in data['edges'] if e['polarity'] == p) for p in sorted(polarity)} }")
    print(f"  已核 QID 条目：{qid_filled}")

    if violations:
        print(f"\nFAIL：{len(violations)} 项违例")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("\nPASS：种子结构契约全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
