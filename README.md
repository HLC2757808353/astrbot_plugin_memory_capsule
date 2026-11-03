# 记忆胶囊 v0.31.0

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.31.0-red.svg)]()

*长期记忆 · 人物档案 · 梗库 · 说话方式——一个面板全管*

</div>

---

## 这是什么

给 AstrBot 装一个"小本本"：她自己记事情，也自己学怎么说话。

四个能力，各自独立、可以单独关掉：

| 能力 | 记什么 | 怎么用上 |
|---|---|---|
| **记忆** | 事实、约定、偏好、待办（重要度 1-10） | 每轮对话按相关度自动匹配，命中才注入 |
| **人物档案** | 昵称（+ 曾用名）、关系、印象、备注 | 每轮都注入"正在和你说话的这个人"；见到就自动建档 |
| **梗库** | 黑话、谐音梗、抽象梗（释义/出处/例句） | 用户消息里出现该词才注入，每条计数 |
| **说话方式** | 从你们的对话里提炼的规则 + 场景示范 | 规则常驻注入，示范按话题匹配 |

> **没有向量检索，也没有 RAG。** 检索走的是 SQLite FTS5 + jieba 分词 + 标签/TF-IDF 多路召回，再用 RRF 融合、MMR 去重、时效衰减排序——纯本地、无 embedding、不依赖向量库。（早期确实设想过 RAG 方向，已放弃。）

---

## 每轮对话她实际看到什么

注入是**临时拼接**的：每次请求重新生成，拼进 system prompt，**不会写进会话历史**，所以不会随对话轮数累积。

优先级从高到低：

| 顺序 | 块 | 何时出现 | 上限 |
|---|---|---|---|
| 1 | `<梗/黑话释义>` | 消息里命中梗库的词 | `glossary_inject_limit` = 3 条 |
| 2 | `<对当前对话对象的了解>` | 有该用户的档案时，**每轮都有** | 印象/备注各截断 120 字，曾用名最多 3 个 |
| 3 | `<群聊近况>` | 群聊且最近有人说过话 | `group_context_lines` = 3 人，每人 1 条，每条 ≤20 字 |
| 4 | `<说话方式>` | 学到过规则/示范时 | 规则 6 条 + 示范 2 条，整块 ≤900 字 |
| 5 | `<可能相关的历史记忆>` | 词面重合度达标时 | `memory_auto_inject_limit` = 3 条 |

`<群聊近况>` 只给事实（谁、ID、说了什么），不含任何"该怎么回"的要求——它是用来防止把别人的事记到当前对话者头上，不是用来教她说话。

实测一轮全开约 **300-700 字（≈250-600 tokens）**，比一条普通图片描述还便宜。

几个刻意的设计：

- 只保留"防误解"和"防工具打架"的说明，不写任何"该怎么说话"的要求
- 记忆块会写"这些已经给你了，不需要再调用 search_memory 查同一件事"
- 说话方式块会写"这是你学到的习惯，不是用户这轮提出的新要求"
- 检索没结果时，工具返回里带一句"不代表你不知道，优先依据上下文"

---

## AI 能用的工具（8 个）

| 工具 | 用途 |
|---|---|
| `write_memory` | 记一条记忆（重要度、public/private） |
| `search_memory` | 多路召回 + RRF/MMR 排序，支持分类、标签过滤 |
| `get_all_memories` | 按重要度/分类列出记忆 |
| `delete_memory` | 删除记忆 |
| `update_relationship` | 写入/更新人物档案（昵称、关系、印象、备注、初遇地点） |
| `search_relationship` | 模糊找人 |
| `get_all_relationships` | 列出所有档案 |
| `delete_relationship` | 删除档案 |

工具查不到东西时**不会装作失忆**——返回里会带上兜底提示，避免模型回"我没有记录"。

---

## 记忆的生命周期

```
写入 → 被用到（注入/搜索都算） → 变冷 → 降权 → 归档 → 仍可搜索/一键恢复
```

**① 排序衰减**（不删任何东西）

```
得分 = 相关度 × 重要度加成 × 时效系数
时效系数 = 0.5 ^ (空闲天数 / 半衰期)
半衰期： 重要度 ≥8 → 不衰减 ｜ 5-7 → 90 天 ｜ ≤4 → 30 天
```

重要度高的记忆有"地板"，不会被时间压下去（生日、雷点这类一年用一次，但必须记住）。

**② 归档**

重要度 ≤4 且 `memory_archive_days`（默认 **60 天**）没被用到 → 打上"已归档"：

- 不再参与自动注入（省上下文）
- **显式搜索仍然找得到**
- 面板里一键恢复

**③ 清理是归档，不是删除**

旧版本的清理会 `DELETE`，而访问次数只在显式搜索时增加——一条天天被自动注入的记忆，看起来像"从没用过"，一年后就被删了。现在：

- 清理 = 归档，可恢复
- 重要度 ≥ `memory_cleanup_protect_importance`（默认 6）**永不自动清理**

---

## 公私分层：哪些记忆不能在群里说

这是这个插件和普通记忆插件最不一样的地方。

- `public`（默认）：任何场景都能被自动注入——**属于大家的记忆**
- `private`：只在**和记忆归属人私聊**时注入；群聊里永不出现，但仍可被显式搜索

`write_memory` 有 `visibility` 参数，敏感内容会自动判定为 private（关键词命中）。群聊里注入的档案也只针对当前发言人，并附带"不要套用到群里其他成员身上"。

---

## 梗库：自己收集网上的新词

- 数据源（公开热榜，无需登录）：B站热门、B站热词、百度热搜、头条热榜
- 每 `glossary_collect_interval_hours`（默认 **72 小时**）跑一次，每次最多入库 `glossary_collect_max_per_run`（8）条
- 用 LLM 提炼：**必须**给出释义、出处/场景、例句；标题党、空解释、编造的一律丢弃
- 去重三层：规范化精确匹配 → 编辑距离（相似度 ≥0.75 合并）→ 存量批量扫描
- 命中计数：只统计**真正注入**的条目（超上限被丢弃的不算），卡片显示 `命中 N`

---

## 说话方式：她怎么学会"好好说话"

放养式，不需要你一条条教。

**采样**：每轮把"用户说了啥 / 她怎么回的"记进 `learn_turns`（不花模型调用，7 天自动清理）。

**触发**：
- 每 `learn_review_hours`（默认 **24 小时**）回看一次
- 或你一纠正她（"太官方了""别这么列点"）立刻学一次，每天上限 30 次

**提炼**：一次模型调用，产出两类东西

- `rule`：可执行的规则（"别用首先/其次列点""安慰人先接情绪再给结论"）
- `example`：场景示范（什么情况下、原来那样回不好、更好的回法）

**约束**：只保留有证据的（被纠正过、明确表达过偏好、反复出现）；涉及隐私、"服从/听命"类内容一律丢弃；新规则若推翻旧规则，旧规则自动下线。

**面板可管**：「学习 → 说话方式」里逐条查看、删除；删了就不再注入。

---

## 面板

AstrBot 面板 → 插件 → 记忆胶囊。五个标签：

```
仪表盘 ｜ 记忆宫殿 ｜ 关系图谱 ｜ 学习 ｜ 设置
                            ├─ 说话方式
                            ├─ 梗百科（搜索/分类/批量/导入导出/查重/清理自动采集）
                            └─ 学习节奏（周期、上限、待学轮次、立即学习/采集）
```

几个实用功能：

- 记忆/梗都支持**多选批量删除**
- 梗库的「检查重复」：预览相似分组，确认后一键合并
- 记忆页「显示已归档」：查看归档条目并恢复
- 设置页所有改动**立即生效并写盘**（不再是"重启就回退"）

---

## 检索是怎么工作的（不涉及向量）

因为相关度必须便宜且可解释，走的是传统检索：

1. **召回三路**：FTS5 全文 / 标签 / TF-IDF；都没结果时补一路 LIKE 兜底
2. **融合**：RRF（倒数排名融合，`rrf_k` 可调）
3. **去重**：MMR 抑制同质结果（`mmr_lambda` 可调）
4. **重排**：重要度加成 × 时效衰减

没有 embedding、没有向量库、没有外部检索服务。

---

## 配置分组（48 项，全部可在面板改）

| 分组 | 关键项 |
|---|---|
| 记忆功能 | `memory_palace`、`identity_mapping_enabled`、`memory_categories` |
| 注入 | `auto_inject_enabled`、`memory_auto_inject_enabled/limit/min_score`、`context_inject_position`、`glossary_inject_limit` |
| 关系 | `relation_injection_refresh_time`、`relation_cache_ttl`、`max_aliases_per_user`（曾用名上限，默认 3） |
| 群聊近况 | `group_context_enabled`、`group_context_lines`、`group_context_window_minutes` |
| 检索排序 | `search_max_results`、`rrf_k`、`mmr_enabled`、`mmr_lambda`、`tfidf_search_enabled`、`tfidf_search_limit`、`max_extracted_tags` |
| 衰减与归档 | `memory_decay_enabled`、`memory_archive_enabled/days/max_importance` |
| 清理保护 | `memory_cleanup_enabled/days/max/protect_importance` |
| 自动学习 | `learn_enabled`、`learn_model`、`learn_review_hours`、`learn_max_rules`、`learn_max_examples`、`learn_max_calls_per_day` |
| 梗库采集 | `glossary_collect_enabled/interval_hours/llm_model/max_per_run` |
| 性能 | `lightweight_mode`、`category_model`、`time_offset` |
| 备份 | `backup_interval`、`backup_max_count` |
| 独立 WebUI | `webui_enabled/host/port`（默认关，用 AstrBot 内嵌页面） |

---

## 安装

AstrBot 插件市场搜索「记忆胶囊」，或在面板「插件」里装。

依赖都是可选的，缺了会自动降级：

| 依赖 | 用途 | 缺失时 |
|---|---|---|
| `jieba` | 中文分词 | 退化为正则二元切片，仍能匹配 |
| `httpx` | 抓热榜 | 跳过自动采集，不影响其他功能 |

```bash
pip install jieba httpx
```

---

## 数据存储

```
AstrBot/data/plugin_data/memory_capsule/
├── memory.db                  ← 所有数据（更新插件不丢）
└── memory_capsule_backups/    ← 自动备份（默认每 24h，保留 10 份）
```

| 表 | 用途 |
|---|---|
| `memories` | 长期记忆（含 visibility / archived / access_count） |
| `relationships` | 人物档案（昵称、关系、印象、备注、别名） |
| `glossary` | 梗库（释义、出处、命中次数） |
| `style_rules` | 学到的规则与场景示范 |
| `learn_turns` | 待学习的对话轮次（7 天） |
| `activities` | 操作记录 |

---

## 常见问题

**她怎么记不住刚说过的话？**
先看记忆页有没有这条，再看「可能相关」是否命中——自动注入要求词面重合度达标（`memory_auto_inject_min_score`，默认 2 个词），太短的消息不注入是刻意的，避免硬塞无关内容。

**为什么群里她不提某人的私事？**
`private` 记忆只在私聊注入。这是设计，不是 bug。

**某条记忆突然不出现了？**
可能是被归档了。记忆页开「显示已归档」看看，点恢复即可。

**学到的规则不太对？**
「学习 → 说话方式」里删掉那一条，之后就不再注入。或者把 `learn_max_rules` 调小。

**面板改了设置会丢吗？**
不会，会立即生效并写进插件的配置文件。
