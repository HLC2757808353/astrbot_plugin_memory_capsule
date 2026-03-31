# 记忆胶囊插件

<div align="center">

[![AstrBot 版本](https://img.shields.io/badge/AstrBot-v4.x-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python 版本](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)
[![许可证](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

*一个为 AstrBot 打造的长期记忆与关系管理系统*

</div>

---

## 📖 简介

记忆胶囊是一个强大的 AstrBot 插件，为 AI 提供**持久化记忆存储**和**智能关系管理**能力。让 AI 能够在多轮对话中记住重要信息，并在适当的时候检索和利用这些记忆。

### 核心特性

- 🧠 **记忆宫殿** - 像人类一样存储和检索长期记忆
- 🔗 **关系图谱** - 记录用户印象，管理社交关系
- 🔍 **智能搜索** - 语义匹配 + 标签 + 分类 + 拼音模糊搜索
- 💉 **上下文注入** - 自动将记忆注入 AI 对话上下文
- 🎨 **WebUI 管理** - 可视化管理和配置插件
- 🔄 **自动备份** - 数据库自动备份，防止数据丢失

---

## ✨ 功能详解

### 1. 记忆宫殿 (Memory Palace)

让 AI 能够"记住"重要的事情，像人类的长期记忆一样存储和检索。

**AI 可用的工具：**

| 工具名 | 功能 | 说明 |
|--------|------|------|
| `write_memory` | 存储记忆 | 自动提取标签和分类 | |
| `search_memory` | 搜索记忆 | 支持多维度精确检索 |
| `delete_memory` | 删除记忆 | 移除不需要的记忆 |
| `get_all_memories` | 获取所有记忆 | 查看记忆库内容 |

**智能特性：**
- 🔖 **自动标签提取** - 使用 jieba 分词自动提取关键词作为标签
- 🏷️ **自动分类** - 可选 LLM 自动分类（需配置分类模型）
- 📅 **时间衰减** - 越新的记忆权重越高
- 🔤 **拼音模糊搜索** - 输入 `beijing` 也能找到「北京」

**搜索策略：**
```
支持 AND/OR 匹配模式
同义词扩展（如：电脑 ↔ 计算机）
分类过滤
自动回退机制
```

---

### 2. 关系图谱 (Relationship Graph)

记录 AI 与用户的交互历史，构建"社交记忆"。

**AI 可用的工具：**

| 工具名 | 功能 | 说明 |
|--------|------|------|
| `update_relationship` | 更新关系 | 记录印象、昵称、关系类型等 |
| `search_relationship` | 搜索关系 | 通过 ID、昵称、关系类型等搜索 |
| `get_all_relationships` | 获取所有关系 | 查看关系库 |
| `delete_relationship` | 删除关系 | 移除关系记录 |

**关系信息包括：**
- 👤 用户 ID
- 📝 AI 给用户的昵称
- 💭 关系类型（朋友、陌生人、熟人...）
- 📍 初次见面地点
- 🏠 多次相遇群组
- 🎯 核心印象总结

**自动上下文注入：**
每次对话时，AI 会自动获取当前用户的关系信息并注入到上下文中，无需手动触发。

**注入方式（可配置）：**
- 系统提示词追加
- 用户提示词前置
- 上下文列表插入

**缓存机制：**
- 用户切换立即刷新
- 同一用户按时间间隔刷新（默认 1 小时）

---

### 3. WebUI 管理界面

提供可视化的管理面板，方便查看和管理记忆与关系。

**访问地址：** `http://localhost:5000`（端口可配置）

**功能模块：**

```
📚 记忆管理
   ├── 浏览所有记忆
   ├── 查看记忆详情
   ├── 测试搜索功能
   └── 记忆统计分析

🔗 关系管理
   ├── 查看所有关系
   ├── 关系详情
   └── 关系搜索

⚙️ 系统配置
   ├── 搜索权重调整
   ├── 搜索策略配置
   └── 注入方式设置

💾 数据管理
   ├── 数据库备份
   └── 备份恢复
```

---

### 4. 跨插件数据存储

其他插件可以通过接口在记忆胶囊中存储和查询数据。

```python
from astrbot_plugin_memory_capsule import store_plugin_data, query_plugin_data

# 存储数据
store_plugin_data("笔记内容", metadata={"type": "note", "tags": ["工作"]})

# 查询数据
results = query_plugin_data("关键词")
```

---

## 🚀 安装

### 方式一：从源码安装

```bash
# 克隆插件仓库
git clone https://github.com/HLC2757808353/astrbot_plugin_memory_capsule.git

# 将插件目录复制到 AstrBot 插件目录
cp -r astrbot_plugin_memory_capsule <你的AstrBot路径>/data/plugins/
```

### 方式二：从插件市场安装

在 AstrBot WebUI 的插件管理页面搜索「记忆胶囊」并安装。

---

## 📦 依赖

### 必需依赖

```bash
pip install jieba pypinyin
```

### 可选依赖

```bash
# 字符串相似度计算（提升搜索准确性）
pip install python-Levenshtein

# 缓存序列化加速
pip install msgpack
```

---

## ⚙️ 配置

插件支持丰富的配置项，通过 `metadata.yaml` 同级的 `_conf_schema.json` 文件或 WebUI 进行配置。

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `webui_port` | int | 5000 | WebUI 服务端口 |
| `memory_palace` | bool | true | 是否启用记忆宫殿 |
| `relation_injection` | bool | true | 是否启用关系注入 |
| `context_inject_position` | string | user_prompt | 注入方式 |
| `relation_injection_refresh_time` | int | 3600 | 关系注入刷新间隔（秒） |

### 记忆宫殿配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `category_model` | string | "" | 自动分类使用的模型 ID |
| `max_extracted_tags` | int | 10 | 自动提取的最大标签数 |
| `search_default_limit` | int | 5 | 默认搜索结果数量 |

### 搜索配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `search_weights` | object | 见下方 | 搜索权重配置 |
| `search_strategy` | object | 见下方 | 搜索策略配置 |
| `max_cache_size` | int | 1000 | 缓存大小限制 |

**默认搜索权重：**
```json
{
  "tag_match": 5.0,
  "recent_boost": 3.0,
  "mid_boost": 2.0,
  "popularity": 1.0,
  "category_match": 2.0,
  "full_match_bonus": 10.0
}
```

**默认搜索策略：**
```json
{
  "match_type": "AND",
  "synonym_expansion": true,
  "time_decay": true,
  "category_filter": false,
  "enable_fallback": true
}
```

### 备份配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `backup_interval` | int | 24 | 自动备份间隔（小时） |
| `backup_max_count` | int | 10 | 最大备份保留数量 |

---

## 🤖 AI 使用指南

### 触发记忆存储

当 AI 判断需要记住某事时，可以调用 `write_memory` 工具：

```
用户：Python 是一种广泛使用的高级编程语言
AI：（调用 write_memory 存储这个知识点）
```

### 检索记忆

需要回忆某些信息时：

```
用户：你之前跟我提过什么关于 Python 的知识？
AI：（调用 search_memory 搜索"Python"）
```

### 更新用户印象

每次交互后，AI 可以更新对用户的印象：

```
用户：我今天学会了用 Python 写爬虫
AI：（调用 update_relationship 更新印象）
```

---

## 🔧 故障排除

### WebUI 无法访问

1. 检查端口是否被占用：`netstat -ano | grep 5000`
2. 查看日志中的错误信息
3. 修改 `webui_port` 配置为其他端口

### 搜索结果不准确

1. 调整 `search_weights` 权重配置
2. 尝试不同的 `search_strategy`
3. 增加 `search_default_limit` 限制

### 依赖缺失

```bash
# 安装必需依赖
pip install jieba pypinyin

# 安装可选依赖
pip install python-Levenshtein msgpack
```

---

## 📝 更新日志

### v0.7.9
- 新增智能标签提取（基于 jieba 分词）
- 新增拼音模糊搜索
- 优化搜索权重系统
- 新增同义词扩展功能
- WebUI 界面重构
- 新增搜索策略配置
- 关系注入缓存优化

### 早期版本
- 基础记忆存储和检索
- 关系图谱功能
- 基础 WebUI
- 自动上下文注入

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

## 📄 许可证

MIT License

---

## 👤 作者

**引灯续昼**

- GitHub: [HLC2757808353](https://github.com/HLC2757808353)
- 项目地址: [astrbot_plugin_memory_capsule](https://github.com/HLC2757808353/astrbot_plugin_memory_capsule)

---

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 强大的聊天机器人框架
- [jieba](https://github.com/fxsjy/jieba) - 中文分词库
- [pypinyin](https://github.com/mozillazg/python-pinyin) - 拼音转换库

---

<div align="center">

*如果这个插件对你有帮助，欢迎 Star ⭐*

</div>
