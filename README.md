# 记忆胶囊 v0.13.0

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.23+-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.13.0-red.svg)]()

*让 AI 拥有持久记忆 · 智能关系管理 · 安全防护*

</div>

---

## 这是什么？

记忆胶囊是 AstrBot 的记忆增强插件。装上之后，AI 就能：

- **记住你说过的话** — 知识、笔记、网址，随时存随时查
- **记住你是谁** — 昵称、关系、印象、约定，跨群追踪
- **自动回忆** — 每次对话自动注入相关记忆，AI 不再金鱼脑
- **安全可靠** — 防注入、防操纵、Token 认证，别人骗不了 AI

---

## 有什么功能？

### 🧠 两层记忆架构

| 层级 | 机制 | 说明 |
|------|------|------|
| **工作记忆** | 自动注入 | 每次对话自动检索最相关的记忆注入上下文，AI 无需主动搜索 |
| **长期记忆** | 按需搜索 | AI 通过 `search_memory` 工具主动检索，用于回答具体问题 |

工作记忆怎么检索的？三步走：
1. **快速筛选** — 核心记忆（importance≥8）+ 最近记忆 + FTS5全文匹配 + 标签匹配
2. **精细评分** — 重要性×2 + 时间衰减 + 访问频率 + 上下文关键词重叠度 + 标签命中
3. **联想扩散** — 被激活的记忆通过共享标签触发关联记忆（类似人脑的联想回忆）

### 🔗 关系图谱

- **人物档案** — 昵称、关系类型、印象总结、初次见面地点
- **多群追踪** — 同一用户在不同群的身份自动关联（新群追加，不覆盖旧的）
- **身份解析** — 精确ID / 昵称 / 别名 / 模糊匹配，四级识别
- **智能注入** — 对话时自动将对方关系信息注入AI上下文，XML标签格式，紧凑不占位

### 🔍 搜索引擎

- **FTS5 全文索引** — 毫秒级检索，支持中文分词（jieba）
- **MMR 多样性筛选** — 不会返回一堆相似结果，保证信息覆盖面
- **标签系统** — 自动提取关键词标签，支持标签检索和联想扩散
- **智能回退** — FTS5不可用时自动降级到LIKE搜索，不会报错

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

针对小内存服务器（300MB）做了专门优化：

| 优化项 | 方案 |
|--------|------|
| 数据库连接 | 线程本地连接池 + WAL模式，不反复创建连接 |
| 只读操作 | SELECT 后不 commit，减少磁盘IO |
| 工作记忆 | 60秒 TTL 缓存，不每次请求都查库 |
| 注入去重 | 30秒内相同内容跳过注入 |
| 备份等待 | `threading.Event` 替代循环 `sleep(1)` |

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
| `search_memory` | 搜索记忆 | FTS5全文搜索 + MMR多样性筛选 |
| `delete_memory` | 删除记忆 | 删除过时或错误的记忆 |
| `update_relationship` | 记录关系 | 昵称/关系类型/印象/约定，多群追加 |
| `search_relationship` | 搜索关系 | 按ID/昵称/类型/印象搜索 |
| `get_all_relationships` | 关系列表 | 获取所有关系的ID和昵称概览 |
| `delete_relationship` | 删除关系 | 删除某个用户的关系记录 |

**无需手动触发** — AI 会根据对话内容智能判断是否需要调用！

### 注入位置

`context_inject_position` 控制记忆信息注入到 AI 上下文的位置：

| 选项 | 效果 | 适用场景 |
|------|------|----------|
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
| `working_memory_limit` | 6 | 工作记忆条数上限 |
| `working_memory_max_chars` | 800 | 工作记忆总字符上限 |
| `context_inject_position` | system_prompt | 注入位置 |
| `relation_injection_refresh_time` | 3600 | 关系注入刷新间隔(秒)，-1=每次都注入 |
| `search_max_results` | 5 | 搜索返回条数 |
| `mmr_enabled` | true | MMR多样性筛选 |
| `mmr_lambda` | 0.7 | MMR相关性/多样性平衡(0-1) |
| `cache_ttl` | 300 | 缓存过期(秒) |
| `max_cache_size` | 200 | 缓存条数上限 |
| `memory_cleanup_enabled` | true | 自动清理旧记忆 |
| `memory_cleanup_days` | 365 | 清理多少天前的低价值记忆 |
| `memory_cleanup_max` | 10000 | 记忆总数上限 |
| `backup_interval` | 24 | 自动备份间隔(小时) |
| `backup_max_count` | 10 | 备份文件保留数量 |
| `webui_port` | 5000 | WebUI端口 |
| `category_model` | "" | 分类模型ID（留空则用规则分类） |
| `memory_categories` | [...] | 自定义分类列表 |

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

1. 检查端口：`netstat -ano | findstr 5000`
2. 查看日志错误信息
3. 修改 `webui_port` 配置后重启
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
2. 调整 `mmr_lambda`（降低=更多样，升高=更相关）
3. 增加 `working_memory_limit`
</details>

---

## 作者

**引灯续昼** — GitHub: [@HLC2757808353](https://github.com/HLC2757808353)

---

<div align="center">

**觉得有用就给个 ⭐**

</div>
