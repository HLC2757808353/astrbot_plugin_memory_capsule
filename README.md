# 记忆胶囊 v0.14.0

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.23+-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.14.0-red.svg)]()

*让 AI 拥有持久记忆 · 知识图谱 · 梦境模式 · RRF融合检索*

</div>

---

## 这是什么？

记忆胶囊是 AstrBot 的轻量记忆增强插件。装上之后，AI 就能：

- **记住你说过的话** — 知识、笔记、网址，随时存随时查
- **记住你是谁** — 昵称、关系、印象、约定，跨群追踪
- **构建知识图谱** — 实体-关系-实体三元组，图遍历检索关联知识
- **夜间做梦** — 回顾一天记忆，发现模式，生成洞察
- **自动回忆** — 每次对话自动注入相关记忆，AI 不再金鱼脑
- **安全可靠** — 防注入、防操纵、Token 认证，别人骗不了 AI

> 不依赖向量数据库，纯 SQLite + FTS5 + BM25 + RRF，轻量到 300MB 内存的服务器都能跑

---

## 有什么功能？

### 🧠 两层记忆架构

| 层级 | 机制 | 说明 |
|------|------|------|
| **工作记忆** | 自动注入 | 每次对话自动检索最相关的记忆注入上下文，AI 无需主动搜索 |
| **长期记忆** | 按需搜索 | AI 通过 `search_memory` 工具主动检索，用于回答具体问题 |

工作记忆怎么检索的？**RRF 多路融合**：

1. **多路召回** — 核心记忆（importance≥8）+ 最近记忆 + BM25全文匹配 + 标签匹配
2. **RRF融合** — 四路结果通过 Reciprocal Rank Fusion 合并排序，`score = Σ 1/(k + rank)`
3. **精细评分** — 重要性×2 + 时间衰减 + 访问频率 + 上下文关键词重叠度 + 标签命中
4. **联想扩散** — 被激活的记忆通过共享标签触发关联记忆（类似人脑的联想回忆）

### 🔍 BM25 + RRF 检索引擎

不用向量数据库也能打出高质量检索：

| 技术 | 作用 |
|------|------|
| **FTS5 + BM25** | SQLite 内置全文索引 + BM25概率排序，毫秒级检索 |
| **RRF 融合** | 多路检索结果（全文/标签/核心/最近）融合排序，比单路更准 |
| **MMR 多样性** | 去重相似结果，保证信息覆盖面 |
| **jieba 分词** | 中文分词提升 FTS5 搜索质量 |
| **智能回退** | FTS5 不可用时自动降级到 LIKE 搜索 |

> **为什么不用向量数据库？** SQLite FTS5 + BM25 对文本检索已经足够强，RRF 融合多路结果比单路向量检索更鲁棒，而且零额外依赖。

### 🕸️ 知识图谱

轻量三元组存储，图遍历检索：

- **add_knowledge** — 添加实体关系三元组（主体-谓词-客体），如 `Python → 是 → 编程语言`
- **search_knowledge** — 图遍历搜索，从实体出发沿关系扩展，支持深度1-2
- **自动关联** — 记忆删除时自动清理关联三元组
- **索引优化** — subject/predicate/object 三列索引，查询飞快

### 💭 梦境模式

让 AI 每晚"做梦"——回顾一天记忆，发现模式，生成洞察：

1. AI 调用 `dream` → 获取当天重要/高频记忆作为"梦境素材"
2. AI 自由回顾这些记忆，发现隐藏的模式和关联
3. AI 调用 `save_dream` → 保存梦境日志（摘要 + 洞察）
4. 梦境日志按天存储，可回溯查看

> 灵感来源：人类睡眠时的记忆巩固——海马体在夜间重放白天经历，强化重要记忆，建立新的关联

### 🔗 关系图谱

- **人物档案** — 昵称、关系类型、印象总结、初次见面地点
- **多群追踪** — 同一用户在不同群的身份自动关联（新群追加，不覆盖旧的）
- **身份解析** — 精确ID / 昵称 / 别名 / 模糊匹配，四级识别
- **智能注入** — 对话时自动将对方关系信息注入AI上下文，XML标签格式，紧凑不占位

### 🛡️ 安全防护

三层安全，防止恶意操纵：

1. **内容过滤** — 20+种 Prompt 注入模式检测（中英文），"忽略你的指令"、"jailbreak" 这类直接拦截
2. **关系过滤** — 6种操纵性内容检测，"你是我的奴隶" 这类自动重置为正常关系
3. **内容清洗** — 移除 `<system>`、`[system]` 等伪装标签，单条记忆限500字符

### 🌐 WebUI 管理面板

- **可视化操作** — 浏览、搜索、编辑记忆和关系
- **动态配置** — 所有配置项在前端按分组展示，开关/下拉/数字/文本都有
- **Token 认证** — 32位混合字符（大小写+数字+符号），每次重载重新生成，日志只显示一次
- **备份管理** — 一键备份/恢复/删除

### ⚡ 性能优化

插件自身运行内存占用极低，适合 2GB 内存的服务器：

| 模式 | 内存占用 | 说明 |
|------|----------|------|
| **轻量模式** | ~1MB | 禁用 jieba + pypinyin，标签用正则提取 |
| **标准模式** | ~12MB | 启用 jieba（11MB），禁用 pypinyin |
| **完整模式** | ~46MB | 启用 jieba（11MB）+ pypinyin（35MB） |

> 默认配置：jieba 启用，pypinyin 禁用（≈12MB）。开启轻量模式可降至 ≈1MB

| 优化项 | 方案 |
|--------|------|
| 数据库连接 | 线程本地连接池 + WAL模式，不反复创建连接 |
| 只读操作 | SELECT 后不 commit，减少磁盘IO |
| 工作记忆 | 60秒 TTL 缓存，不每次请求都查库 |
| 注入去重 | 30秒内相同内容跳过注入 |
| 备份等待 | `threading.Event` 替代循环 `sleep(1)` |
| pypinyin | 默认禁用（46MB），需要时手动开启 |
| 轻量模式 | 一键禁用 jieba+pypinyin，正则分词降级 |

---

## 怎么用？

### 安装

```bash
# 方式1: 插件市场
# 在 AstrBot 管理面板搜索「记忆胶囊」安装

# 方式2: 手动安装
git clone https://github.com/HLC2757808353/astrbot_plugin_memory_capsule.git
# 复制到 AstrBot/data/plugins/ 目录
```

### 依赖

```bash
pip install cachetools jieba pypinyin
```

> `jieba` 和 `pypinyin` 可选，不装也能用（搜索和标签功能降级）

### 启动后

1. 查看 AstrBot 日志，找到 WebUI Token（32位，只显示一次）
2. 浏览器访问 `http://localhost:5000`
3. 输入 Token 登录
4. 在设置页面调整配置

> 忘记 Token？重启插件即可生成新 Token

### AI 怎么用？

插件自动为 AI 注册以下工具，AI 根据对话自主决定何时调用：

| 工具 | 用途 | 说明 |
|------|------|------|
| `write_memory` | 记录客观信息 | 知识/笔记/网址，自动分类+标签+重要性评估 |
| `search_memory` | 搜索记忆 | BM25全文搜索 + RRF融合 + MMR多样性筛选 |
| `delete_memory` | 删除记忆 | 删除过时或错误的记忆 |
| `add_knowledge` | 添加知识 | 实体-关系-实体三元组，构建知识图谱 |
| `search_knowledge` | 搜索知识 | 图遍历检索关联实体和关系 |
| `dream` | 进入梦境 | 回顾当天记忆，发现模式和洞察 |
| `save_dream` | 保存梦境 | 保存梦境回顾的摘要和洞察 |
| `update_relationship` | 记录关系 | 昵称/关系类型/印象/约定，多群追加 |
| `search_relationship` | 搜索关系 | 按ID/昵称/类型/印象搜索 |
| `get_all_relationships` | 关系列表 | 获取所有关系的ID和昵称概览 |
| `delete_relationship` | 删除关系 | 删除某个用户的关系记录 |

**无需手动触发** — AI 会根据对话内容智能判断是否需要调用！

### 批量导入

通过 WebUI API 批量导入 JSON 数据：

```bash
curl -X POST http://localhost:5000/api/import \
  -H "Content-Type: application/json" \
  -H "X-Session-Token: YOUR_TOKEN" \
  -d '{
    "memories": [
      {"content": "Python是解释型语言", "category": "技术笔记", "importance": 5},
      {"content": "用户喜欢猫", "category": "个人想法", "importance": 3}
    ]
  }'
```

支持字段：`content`（必填）、`category`、`importance`、`tags`、`source`。单次最多 500 条，自动去重。

### 被动记忆注入

插件会在每次对话时自动根据用户消息检索相关记忆并注入 AI 上下文，无需手动触发：

1. 用户发消息 → `inject_context()` 自动触发
2. 根据消息内容 → RRF 多路融合检索（BM25全文 + 标签 + 核心记忆 + 最近记忆）
3. 相关记忆 → 自动注入 AI 上下文（system_prompt/user_prompt/独立消息）
4. 关系信息 → 同步注入（昵称、关系类型、印象、约定等）

> 这就是"被动注入"——AI 不需要主动搜索，相关记忆会自动出现在上下文中

### 注入位置

`context_inject_position` 控制记忆信息注入到 AI 上下文的位置：

| 选项 | 效果 | 适用场景 |
||------|------|----------|
| `system_prompt` | 追加到系统提示词末尾 | 默认，兼容性最好 |
| `user_prompt` | 插入到用户消息前面 | 让 AI 更关注记忆内容 |
| `insert_system_prompt` | 作为独立 system 消息插入 | 多轮对话中更清晰 |

---

## 配置项

在 AstrBot 管理面板或 WebUI 设置页调整：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `memory_palace` | true | 启用记忆宫殿 |
| `identity_mapping_enabled` | true | 启用身份映射 |
| `dream_mode_enabled` | true | 启用梦境模式 |
| `lightweight_mode` | false | 轻量模式（省60MB内存，禁用jieba+pypinyin） |
| `disable_pypinyin` | true | 禁用拼音标签（pypinyin占46MB） |
| `working_memory_limit` | 6 | 工作记忆条数上限 |
| `working_memory_max_chars` | 800 | 工作记忆总字符上限 |
| `context_inject_position` | system_prompt | 注入位置 |
| `relation_injection_refresh_time` | 3600 | 关系注入刷新间隔(秒)，-1=每次都注入 |
| `search_max_results` | 5 | 搜索返回条数 |
| `rrf_k` | 60 | RRF融合参数k，越小越重视高排名 |
| `mmr_enabled` | true | MMR多样性筛选 |
| `mmr_lambda` | 0.7 | MMR相关性/多样性平衡(0-1) |
| `max_extracted_tags` | 6 | 每条记忆最大标签数 |
| `cache_ttl` | 300 | 缓存过期(秒) |
| `max_cache_size` | 200 | 缓存条数上限 |
| `memory_cleanup_enabled` | true | 自动清理旧记忆 |
| `memory_cleanup_days` | 365 | 清理多少天前的低价值记忆 |
| `memory_cleanup_max` | 10000 | 记忆总数上限 |
| `backup_interval` | 24 | 自动备份间隔(小时) |
| `backup_max_count` | 10 | 备份文件保留数量 |
| `webui_host` | 0.0.0.0 | WebUI监听地址 |
| `webui_port` | 5000 | WebUI端口 |
| `category_model` | "" | 分类模型ID（留空则用规则分类） |
| `memory_categories` | [...] | 自定义分类列表 |

---

## 技术架构

```
用户消息 → inject_context() → RRF多路融合检索 → 注入AI上下文
                                        ↓
                              ┌─────────┼─────────┐
                              ↓         ↓         ↓
                           BM25全文   标签匹配   核心记忆
                              ↓         ↓         ↓
                              └──── RRF融合排序 ────┘
                                        ↓
                                   MMR多样性筛选
                                        ↓
                                   联想扩散(标签)
                                        ↓
                                   注入AI上下文
```

### RRF 融合算法

```
score(item) = Σ 1/(k + rank_i)    对每路检索结果
```

- k=60（标准值）：多路结果均匀融合
- k 越小：越重视高排名结果
- k 越大：排名影响越小，融合越均匀

### 知识图谱查询

```
search_knowledge("Python", depth=1)
  → Python → 是 → 编程语言
  → Python → 创始人 → Guido
  → 编程语言 → 有 → 多种范式    (depth=2 扩展)
```

---

## 安全机制

### Token 认证
- 32位随机字符（大小写字母 + 数字 + `!@#$%^&*-_=+`）
- 强制包含四种字符类型，保证强度
- `secrets.compare_digest()` 防时序攻击
- 每次插件重载重新生成，日志只打印一次
- Session 24小时过期

### 内容安全
- **write_memory** — 检测 Prompt 注入 → 拦截；清洗危险标签 → 保存
- **update_relationship** — 检测操纵性内容 → 自动重置为正常关系
- **上下文注入** — `<relationship>` / `<memory>` XML标签包裹，标识为参考数据而非指令

### 数据安全
- SQLite WAL模式 — 读写不互锁，崩溃不丢数据
- 自动备份 — 按间隔备份，超出数量自动清理最旧备份
- 哈希去重 — 相同内容不重复存储

---

## 故障排除

<details>
<summary>WebUI 无法访问</summary>

1. 检查 `webui_host` 是否为 `0.0.0.0`（Docker/云服务器需要）
2. 检查端口：`netstat -ano | findstr 5000`
3. 查看日志错误信息
4. 修改 `webui_port` 配置后重启
</details>

<details>
<summary>忘记 Token</summary>

重启插件，新 Token 会打印在日志中
</details>

<details>
<summary>Gemini API 报错 400</summary>

确保 AstrBot 版本 ≥ v4.23.0
</details>

<details>
<summary>搜索结果不准</summary>

1. 安装 jieba 提升中文分词质量
2. 调整 `rrf_k`（减小=更重视高排名，增大=更均匀融合）
3. 调整 `mmr_lambda`（降低=更多样，升高=更相关）
4. 增加 `working_memory_limit`
</details>

---

## 作者

**引灯续昼** — GitHub: [@HLC2757808353](https://github.com/HLC2757808353)

---

<div align="center">

**觉得有用就给个 ⭐**

</div>
