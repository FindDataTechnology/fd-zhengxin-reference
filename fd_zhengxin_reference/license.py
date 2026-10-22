"""许可门骨架（照 fd-zhesi-reference 的双门纪律）。

两道门分开判定：
- **许可门**（本模块）：静态事实——源条款是否允许入库。未确认即不摄取。
- **可得性门**（后续波次的探测模块）：动态事实——此刻能不能取到。

骨架期判定状态（证据落 docs/data-license.md，2026-10-23 官网一手核验）：
- v-dem：许可事实已核验——CC BY-SA 4.0，入库/快照/署名/查询面合规可行
  （ShareAlike 只在导出/下载面触发；摄取限定 Core 或 Country-Date 纯变体）
- marpor：部分合规但三点未确认（注册流程 / scientific research 边界 /
  再分发授权）→ 未过门不摄取
- china-line：源清单未定 → 未过门不摄取
- wikidata / sep：已确认（CC0 / 只链不抄），与姊妹仓同口径

红线机制（REDLINES / assert_allowed_url）随骨架就位：当前无已判定的禁入源，
后续波次评审出禁入源时只补条目、不加机制。
"""

from __future__ import annotations

from urllib.parse import urlparse

# 许可门的事实记录（评审证据见 docs/data-license.md / docs/sep-coverage.md /
# docs/wikidata-probe-samples/；未过门即不摄取）
LICENSE_REGISTRY: dict[str, dict] = {
    "v-dem": {
        "status": "confirmed",
        "license": "CC BY-SA 4.0",
        "commercial_use": True,
        "constraints": [
            "署名按官方建议引用格式（含 DOI），Suggested Citations 随快照原样保留",
            "ShareAlike：内部存储与查询展示不构成分发；导出/下载面构成分发，须同许可并附署名",
            "摄取限定 Core / Country-Date 纯 V-Dem 变体；Full+Others 的第三方列须单独核验",
        ],
        "evidence": "docs/data-license.md §1（官网 FAQ/数据集页 2026-10-23 核验）",
    },
    "marpor": {
        "status": "pending",
        "license": "自定义 Terms of Use（强制引用；再分发默认禁止、须书面授权）",
        "commercial_use": None,
        "constraints": [
            "引用 MPDS 推荐格式 + DOI，并向 Manifesto Project 回送发表引用",
            "未确认三点：主数据集下载是否强制注册；产品语境是否满足 scientific "
            "research；查询/导出面是否构成 Redistribution（须 WZB 书面确认）",
        ],
        "evidence": "docs/data-license.md §2（Terms of Use 全文抓取，三点未确认）",
    },
    "wikidata": {
        "status": "confirmed",
        "license": "CC0",
        "commercial_use": True,
        "constraints": [],
        "evidence": (
            "https://www.wikidata.org/wiki/Wikidata:Licensing；"
            "SPARQL 探针样本见 docs/wikidata-probe-samples/"
        ),
    },
    "sep": {
        "status": "confirmed",
        "license": "站点条款（no-bulk-copy）",
        "commercial_use": True,
        "constraints": ["只存标题、URL 与短引用；不复制条目正文或摘要"],
        "evidence": (
            "docs/sep-coverage.md（政治思想词 26/26 命中实测）"
            "+ fd-zhesi-reference 同口径（引用层只链不抄）"
        ),
    },
    "china-line": {
        "status": "pending",
        "license": "源清单未定，逐源评审后才能摄取",
        "commercial_use": None,
        "constraints": ["先定源清单，再逐源过许可门"],
        "evidence": "源清单与逐源许可评审待做（后续波次）",
    },
}

# 红线：禁止作为依赖或请求目标的源（只能作为引用文本出现）。骨架期空置。
REDLINES: dict[str, dict] = {}

FORBIDDEN_HOSTS: frozenset[str] = frozenset(
    host for entry in REDLINES.values() for host in entry["hosts"]
)


class LicenseNotConfirmed(RuntimeError):
    """许可门未通过：该源不得入库。"""

    def __init__(self, source: str, reason: str = ""):
        entry = LICENSE_REGISTRY.get(source)
        detail = reason or (
            "许可未确认（LICENSE_REGISTRY 无允许判定）"
            if entry is None
            else f"状态={entry['status']}（{entry['license']}）"
        )
        super().__init__(f"许可门未通过：{source} 不摄取（{detail}）")
        self.source = source
        self.reason = detail


class ForbiddenSource(RuntimeError):
    """出站目标命中许可红线。"""

    def __init__(self, url: str, redline: str, reason: str):
        super().__init__(f"许可红线：{url} 属于 {redline}（{reason}），禁止请求")
        self.url = url
        self.redline = redline


def license_entry(source: str) -> dict | None:
    return LICENSE_REGISTRY.get(source)


def license_confirmed(source: str) -> bool:
    entry = LICENSE_REGISTRY.get(source)
    return bool(entry and entry["status"] == "confirmed")


def not_ingested_reason(source: str) -> tuple[str, str | None]:
    """未摄取原因：(reason, gate_detail)。

    未过许可门 → ("license_unconfirmed", 待办原因)；已过门未摄取 →
    ("not_ingested", None)。台账与占位工具据此给出缺口原因，不静默为空。
    """
    entry = LICENSE_REGISTRY.get(source)
    if entry is None:
        return "not_ingested", "源未登记"
    if entry["status"] == "confirmed":
        return "not_ingested", None
    return "license_unconfirmed", entry["license"]


def require_license(source: str) -> dict:
    """许可门：确认允许才返回条目，否则抛 LicenseNotConfirmed。"""
    entry = LICENSE_REGISTRY.get(source)
    if not entry or entry["status"] != "confirmed":
        raise LicenseNotConfirmed(source)
    return entry


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:  # 畸形 URL 也按不可信处理
        return ""


def redline_for(host: str) -> tuple[str, dict] | None:
    """命中红线的源名与条目；未命中 None。子域按后缀匹配。"""
    h = host.lower()
    for name, entry in REDLINES.items():
        for forbidden in entry["hosts"]:
            if h == forbidden or h.endswith("." + forbidden):
                return name, entry
    return None


def assert_allowed_url(url: str) -> str:
    """出站请求前的守卫：命中红线直接抛错（不静默降级）。"""
    hit = redline_for(host_of(url))
    if hit:
        name, entry = hit
        raise ForbiddenSource(url, name, entry["reason"])
    return url


def redline_report() -> list[dict]:
    """红线清单（供文档/自检与测试读取，避免文档与代码漂移）。"""
    return [
        {
            "source": name,
            "hosts": list(entry["hosts"]),
            "license": entry["license"],
            "reason": entry["reason"],
            "allowed_as": "引用文本（attribution / 链接），不得作为请求目标",
        }
        for name, entry in REDLINES.items()
    ]
