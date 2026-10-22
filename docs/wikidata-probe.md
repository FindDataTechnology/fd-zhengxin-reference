# Wikidata 政治面 SPARQL 通道实测（任务 1.3）

端点 `https://query.wikidata.org/sparql`，自定义 UA `fd-zhengxin-probe/0.1`，直连可用（代理 127.0.0.1:7892 备而未用）。全部样本原文在 `docs/wikidata-probe-samples/`（36 份 JSON）。客户端模式参照 fd-zhesi-reference 的 `wikidata.py`。

## 一、四类实体查询实测

| 类 | 样本文件 | 查询要点 | 结果 |
|---|---|---|---|
| 政治人物 | pol1_occupation_birth_death（occupation=politician，生卒/国籍 20 行）；pol2_p39_tenure_qualifiers（P39 任职+起止 qualifier，20 行）；pol3_party_membership_tie（P102 党籍关联，20 行） | 人物基线属性、任职任期、党籍 | 通 |
| 政党 | party1_alignment_p1387（P1387 政治倾向，20 行）；party2_ideology_p1142（P1142 意识形态+国家+成立，20 行）；party3_china_zh（中文标签+意识形态中文值，20 行） | 政党坐标属性双通道（P1387 连续倾向 / P1142 意识形态实体） | 通 |
| 政治事件 | event1_election_with_winner（选举+P991 获胜者，20 行）；event2_revolution_direct（革命类直查，20 行）；event3_revolution_subclass_closure（子类闭包，7 行）；event4_coup_with_time（政变+时间+国家，20 行） | 事件类定位：Q40231 及子类闭包查法 | 通（subclass 闭包查法必要——直查漏子类） |
| 政治组织/运动 | move1_basic_inception（P571 成立，20 行）；move2_ideology_closure（意识形态闭包，20 行）；move3_follows_partof（P155/P361 谱系，20 行） | 运动谱系属性 | 通 |

类定位侦察：find_class_coup/revolution/movement（子类候选与歧义清单，如 revolution 检索混入任天堂 Wii——需用 Q128561 类闭包而非全文检索）。

## 二、关系极性判据表（属性→极性映射）

设计要求极性四档：继承 / 批判 / 同盟 / 对立。实测可支撑：

| 属性 | 含义 | 全库使用量 | 可推导极性 | 样本 |
|---|---|---|---|---|
| P155 follows | 后继于 | 广泛 | 继承（谱系顺序） | edges_P155_follows_generic（8 行，选举届次链示例） |
| P1365 replaces | 取代 | 广泛 | 对立（政权更替）+继承（结构上接续） | edges_P1365_replaces_states（10 行） |
| P361 part of | 属于…部分 | 广泛 | 同盟（组织隶属） | edges_P361_movement_partof（8 行） |
| P463 member of | 成员 | 广泛 | 同盟 | edges_P463_party_to_org（8 行，政党→国际党际组织） |
| P102 party membership | 党籍 | 广泛 | 同盟（人→党） | verify_prop_dossier 确认语义 |
| P1142 ideology | 意识形态 | 政党面 15,257 | 坐标归属（非极性边，作思潮坐标锚） | edges_P1142_party_ideology（8 行） |
| P1387 political alignment | 政治倾向 | 政党面 3,804 | 坐标归属（连续倾向值） | party1（20 行） |
| P461 opposite of | 相反于 | 52,295（全域） | 对立 | domain_P461_opposite_political（20 行政治域过滤样本） |
| P5004 opposition to | 反对于 | 1,483 | 对立 | domain_P5004_opposition_political（20 行） |
| P7047 enemy of | 敌对于 | 7,625 | 对立（个体级，噪声高） | domain_P7047_enemy_political（20 行，需域过滤） |
| P991 winner | 获胜者 | 选举面 7,904 | 事件→实体关联（非极性） | edges_P991_election_winner（8 行） |
| P3323 disputation with | 争讼于 | 仅 4 | （理论可作批判，量级不可用） | usage 计数 |

## 三、极性缺口（诚实记录）

- **「批判」档无良构属性支撑**：属性检索（search_props_critique，15 行）命中的全是 CriticDB 等外部 ID 属性；P3323 全库仅 4 条。批判关系在 Wikidata 结构性缺失——谱系层的批判边须由参照系其他源（SEP 概念层条目、中国线 curated 谱系）承载，Wikidata 只出不进。
- **「对立」三属性质量分层**：P461 语义最稳但需政治域过滤（否则混入虚构角色对立）；P5004 最贴政治域但量小；P7047 个体恩怨噪声高（样本人名级敌对）。摄取时按 P5004 > P461(域过滤) > P7047(降权) 取信。
- **数据噪声实况**：pol2 样本出现 P39 qualifier 误挂（如宗教人物的「先知」职位被当政治任职拉入）——摄取须以 occupation 收敛为主判据，P39 只作任期补充。

## 四、20 条关系边极性回溯验证

从 edges_*/domain_* 样本合并抽取 20 条（P155×2 / P1365×2 / P361×2 / P463×2 / P1142×2 / P1387×2 / P461×3 / P5004×2 / P991×3），每条均为「QID→QID + 属性 + 推导极性」可回溯形态，清单见 `wikidata-probe-samples/`（文件即证据，摄取器验收时按同一形态抽查）。

## 五、摄取器结论

- 四类实体查询通道全通， subclass 闭包查法是事件类的必要姿势。
- 极性边可用：继承（P155/P1365）、同盟（P361/P463/P102）、对立（P461/P5004/P7047 分层取信）；批判档走其他源。
- 坐标锚：P1142/P1387 双通道（政党面 15k/3.8k 使用量充足）。
