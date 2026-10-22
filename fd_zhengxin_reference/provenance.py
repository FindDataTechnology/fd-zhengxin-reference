"""溯源契约：每个结果携带 provenance（source / stable_id / as_of / attribution / snapshot_version）。

缺失 provenance 即 bug，不做降级——对齐 fd-zhesi-reference（lex 参照系线）的契约。
`snapshot_version`：静态数据集结果回到具体快照（数据集版本 + sha256 前 12 位），
使「同一查询在不同时间给出不同结果」可解释；动态/按需层无快照，该字段为 None。

本模块是**引用型 URL 的唯一落点**：外部站点链接只作为署名/引用文本出现，
绝不作为请求目标。中国线源清单未定，citation URL 暂记 TBD（过许可门时补齐）。
"""

from __future__ import annotations

from datetime import date

from .china_lineage import MODERN_ATTRIBUTION, SEED_ATTRIBUTION

# 引用型 URL（只出现在 attribution / 链接文本里，不得作为 HTTP 请求目标）
CITATION_URLS = {
    "v-dem": "https://www.v-dem.net",
    "marpor": "https://manifesto-project.wzb.eu",
    "wikidata": "https://www.wikidata.org",
    "sep": "https://plato.stanford.edu",
    "china-line": "TBD",  # 中国线源清单待许可门评审后落定
}

ATTRIBUTIONS = {
    "v-dem": (
        "V-Dem Dataset (V-Dem Institute), CC BY-SA 4.0（署名 + ShareAlike）,"
        f" {CITATION_URLS['v-dem']}"
    ),
    "marpor": f"Manifesto Project (MARPOR, WZB), {CITATION_URLS['marpor']}（Terms of Use，未过许可门不摄取）",
    "wikidata": f"Wikidata (CC0), {CITATION_URLS['wikidata']}",
    "sep": f"Stanford Encyclopedia of Philosophy, {CITATION_URLS['sep']} (link-only citation)",
    "china-line": "中国线源（清单待定；过许可门前不摄取）",
    # 两 curated 层：非外部语料摄取，归属文本以 china_lineage 的常量为唯一落点（不漂移）
    "china-seed": SEED_ATTRIBUTION,
    "modern-index": MODERN_ATTRIBUTION,
}


def provenance(
    source: str,
    stable_id: str,
    as_of: str | None = None,
    snapshot_version: str | None = None,
) -> dict:
    return {
        "source": source,
        "stable_id": stable_id,
        "as_of": as_of or date.today().isoformat(),
        "attribution": ATTRIBUTIONS.get(source, source),
        "snapshot_version": snapshot_version,
    }
