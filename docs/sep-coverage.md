# SEP 政治哲学概念覆盖面实测（政心 · 思想史概念层侦察）

- 实测日期：2026-10-23
- 方法：逐词实测，非猜测。复用姊妹产品哲思的 SEP 客户端（`fd-zhesi-reference/fd_zhesi_reference/sep.py`）的同一通道：`GET https://plato.stanford.edu/search/searcher.py?query=<term>`，UA `fd-zhesi-reference/0.1`，超时 8s，直连（本地代理 127.0.0.1:7892 备而未用）。请求间隔 1.5s，共 26 次请求（23 主表词 + 3 补充验证词），全部落在搜索端点，无正文页请求。
- 判定口径：`hit` = 搜索结果页解析出含 `/entries/` 链接的引用性结果（客户端只取 标题+摘要片段+链接）；`no_hit` = 端点正常返回但无可用的 entries 链接；`unreachable/upstream` = 网络层/非 200 异常。本次 26/26 均 `hit`，无异常。

## 一、20 个政治思想核心词命中表

| # | 查询词 | 状态 | 首选条目 | URL | 备注 |
|---|--------|------|----------|-----|------|
| 1 | justice | hit | Justice | https://plato.stanford.edu/entries/justice/ | 顶部精确命中 |
| 2 | sovereignty | hit | Sovereignty | https://plato.stanford.edu/entries/sovereignty/ | 精确命中 |
| 3 | liberty | hit | Positive and Negative Liberty | https://plato.stanford.edu/entries/liberty-positive-negative/ | SEP 无独立 "Liberty" 条目，此即规范条目（Berlin 两分框架） |
| 4 | democracy | hit | Democracy | https://plato.stanford.edu/entries/democracy/ | 精确命中 |
| 5 | legitimacy | hit | Political Legitimacy | https://plato.stanford.edu/entries/legitimacy/ | 规范条目名带 "Political" 前缀 |
| 6 | authority | hit | Authority | https://plato.stanford.edu/entries/authority/ | 精确命中 |
| 7 | rights | hit | Rights | https://plato.stanford.edu/entries/rights/ | 精确命中 |
| 8 | equality | hit | Equality | https://plato.stanford.edu/entries/equality/ | 精确命中 |
| 9 | political representation | hit | Political Representation | https://plato.stanford.edu/entries/political-representation/ | 精确命中 |
| 10 | constitutionalism | hit | Constitutionalism | https://plato.stanford.edu/entries/constitutionalism/ | 精确命中 |
| 11 | revolution | hit | Revolution | https://plato.stanford.edu/entries/revolution/ | 精确命中 |
| 12 | conservatism | hit | Conservatism | https://plato.stanford.edu/entries/conservatism/ | 精确命中；次位混入认识论 conservatism 条目，属同形词噪声 |
| 13 | liberalism | hit | Liberalism | https://plato.stanford.edu/entries/liberalism/ | 精确命中 |
| 14 | socialism | hit | Socialism | https://plato.stanford.edu/entries/socialism/ | 精确命中 |
| 15 | nationalism | hit | Nationalism | https://plato.stanford.edu/entries/nationalism/ | 精确命中 |
| 16 | republicanism | hit | Republicanism | https://plato.stanford.edu/entries/republicanism/ | 精确命中 |
| 17 | utilitarianism | hit | The History of Utilitarianism | https://plato.stanford.edu/entries/utilitarianism-history/ | SEP 无独立 "Utilitarianism" 条目；当代理论形态在 Consequentialism（本次结果第 3 位） |
| 18 | social contract | hit | Contemporary Approaches to the Social Contract | https://plato.stanford.edu/entries/contractarianism-contemporary/ | 主题被拆两条，古典契约传统见补充验证（Contractarianism） |
| 19 | property | hit | Intellectual Property（#1）/ Property and Ownership（#2） | https://plato.stanford.edu/entries/property/ | 排名细节：政治哲学规范条目 Property and Ownership 在第 2 位，引用时应挂 #2 |
| 20 | civil disobedience | hit | Civil Disobedience | https://plato.stanford.edu/entries/civil-disobedience/ | 精确命中 |

## 二、中国政治思想线 3 词命中表

| # | 查询词 | 状态 | 首选条目 | URL | 备注 |
|---|--------|------|----------|-----|------|
| 21 | Confucian political thought | hit（无专条） | Japanese Confucian Philosophy（#1，日本线误召）/ Social and Political Thought in Chinese Philosophy（#3，实际承载） | https://plato.stanford.edu/entries/chinese-social-political/ | 无 "Confucian political thought" 专条；儒家政治思想靠伞形条目 chinese-social-political（内引 El Amine《Classical Confucian Political Thought》）+ 思想家条目（见补充验证 Confucius/Mencius）拼合 |
| 22 | legalism (Chinese philosophy) | hit | Legalism in Chinese Philosophy | https://plato.stanford.edu/entries/chinese-legalism/ | 精确命中专条（2014 初版，2023 修订） |
| 23 | Chinese political philosophy | hit | Social and Political Thought in Chinese Philosophy | https://plato.stanford.edu/entries/chinese-social-political/ | 精确命中伞形专条（覆盖先秦至宋明及近现代政治思想） |

### 补充验证（3 词，实测）

| 查询词 | 状态 | 条目 | URL | 用途 |
|--------|------|------|-----|------|
| contractarianism | hit | Contractarianism | https://plato.stanford.edu/entries/contractarianism/ | 补 social contract 的古典契约传统条目 |
| Confucius | hit | Confucius | https://plato.stanford.edu/entries/confucius/ | 中国线思想家单条目存在性 |
| mencius | hit | Mencius | https://plato.stanford.edu/entries/mencius/ | 同上（民本/仁政叙事的载体） |

## 三、命中率统计

| 口径 | 结果 |
|------|------|
| 端点层命中（解析出 entries 链接） | 23/23 = 100%（另补充验证 3/3） |
| 核心词 20：顶部即精确同名条目 | 15/20 |
| 核心词 20：顶部为规范条目（条目名与查询词不同） | 3/20（liberty / legitimacy / utilitarianism） |
| 核心词 20：规范条目在 #2 位 | 1/20（property） |
| 核心词 20：主题拆分多条目 | 1/20（social contract = contractarianism + contractarianism-contemporary） |
| 核心词 20：usable 专条（前 2 位内） | 20/20 = 100% |
| 中国线 3：直接专条 | 2/3（chinese-legalism、chinese-social-political） |
| 中国线 3：无专条、靠伞形+思想家条目拼合 | 1/3（Confucian political thought） |
| 端点行为异常 | 0（无 unreachable / upstream / 非 200） |

## 四、覆盖缺口清单（需其他源补或需引用层特殊处理）

1. **儒家政治思想无专条（最大缺口）**：SEP 没有 "Confucian political thought" 专条。覆盖靠拼合：伞形条目 chinese-social-political + Confucius / Mencius / Xunzi 等思想家条目。政心参照系里中国线概念（仁政、民本、大同、天命、三代、华夷之辨等）在 SEP 基本无一一对应条目，需其他源（中文学术源/专著书目）补齐，SEP 只能作西方学界视角的补充锚点。
2. **中国线广度未验证完**：本次仅实测 legalism、伞形条目、Confucius、Mencius。结果页相关条目区出现 Mohism 链接（来源：chinese-legalism 结果页引用性文字，未实测）；Daoism 政治面向、近现代中国政治思想（如 chinese-social-political 内提及的 Modern Chinese Political Thought 一节）覆盖深度待后续专项实测。
3. **查询词 ≠ 条目名的映射必须维护**：liberty→liberty-positive-negative、legitimacy→legitimacy（Political Legitimacy）、utilitarianism→utilitarianism-history。直查可得正确结果，但引用层若做「查询词→规范条目」硬映射可避免排名漂移。
4. **property 顶部误召**：直查 property 首位是 Intellectual Property，规范条目 Property and Ownership 在 #2。引用层需硬映射到 `/entries/property/`。
5. **social contract 主题拆分**：需同时挂 contractarianism（古典传统）+ contractarianism-contemporary（当代进路）两条；霍布斯/洛克/卢梭的原典性叙事另散于思想家条目。
6. **同形词噪声**：conservatism 会召入认识论 conservatism（doxastic/epistemic），property 会召入形而上学 intrinsic/extrinsic properties——解析层按 `/entries/` 链接全收即可，但人工核名必要。

## 五、「只链不抄」边界在通道层的确认

- 本次实测完全复用 `fd-zhesi_reference/sep.py`：其唯一网络调用是搜索端点 `GET /search/searcher.py?query=<term>`（`fetch_search_html`）；模块内不存在任何请求 `/entries/<slug>/` 正文页的代码路径。
- 解析函数 `parse_search_results` 仅从结果页 `result_listing` 块提取三样东西：`result_title`（标题）、`result_snippet`（摘要片段——结果页自带的引用性片段，通常为条目目录/首段截断）、`/entries/<slug>/` 链接（URL）。无正文抓取，无正文存储。
- 26 次实测请求全部落在搜索端点，正文页一次未触。政心复用此通道即天然继承该纪律；条目页 URL 只作为外链挂入参照系。
- 探针脚本为一次性工具（/tmp/sep_probe.py、/tmp/sep_probe_extra.py），未进任何仓库；本目录仅存本报告。
