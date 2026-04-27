# 记忆胶囊 v0.17.0

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.23+-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-green.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v0.17.0-red.svg)]()

*RAG混合记忆系统 · 抽取式每日摘要 · TextRank关键词 · 多群全局日报 · 零LLM消耗*

</div>

---

## 记忆晋级管道

```
用户消息 → conversation_logs (原始对话，7天自动清理)
         ↓ (每天自动/手动触发)
       抽取式摘要 + TextRank关键词 (零LLM)
         ↓
       daily_summaries (单群每日摘要，30天)
         ↓ ─── 跨群合并 ──→ daily_global_digest (全局日报)
         ↓ (AI提取重要事实)
       write_memory (长期记忆，importance≥9=永久)
```

---

## 每日总结系统（零LLM消耗！）

**30个群也不用怕，全自动处理。**

### 单群摘要
- AI调用 `daily_summary` 或 `save_dream` 时触发
- 系统自动从 `conversation_logs` 读取当天所有消息
- **抽取式摘要** (TF-IDF句子评分)：自动选出最能代表今日对话的句子
- **TextRank关键词**：基于共现图的排序算法，提取核心话题
- 存到 `daily_summaries` 表（每群每天一行）

### 全局日报
- `save_dream` 时自动处理**所有群组**
- 遍历每个有对话的群 → 各自生成摘要
- 合并所有群的关键词 → 话题Counter → 全局Top15热门话题
- 每个群的摘要拼接 → 全局汇总
- 存到 `daily_global_digest` 表（每天一行）

### 注入上下文
- AI每次对话自动获得当天本群摘要
- AI可通过 `get_daily_summary` 查看历史
- AI可通过 `get_daily_digest` 查看全局动向

---

## AI 工具一览（13个）

| 工具 | 用途 |
|------|------|
| `write_memory` | 记录信息（importance 1-10） |
| `search_memory` | 五路融合搜索（关键词/分类/标签） |
| `delete_memory` | 删除记忆 |
| `daily_summary` | **自动生成本群今日摘要（零LLM）** |
| `get_daily_summary` | 获取历史每日总结 |
| `get_daily_digest` | **获取全局日报（跨群汇总）** |
| `dream` | 梦境回顾 |
| `save_dream` | 保存洞察+触发生成所有群每日摘要 |
| `update_relationship` | 记录关系 |
| `search_relationship` | 搜索关系 |
| `get_all_relationships` | 关系列表 |
| `delete_relationship` | 删除关系 |
| `add_knowledge` | 知识三元组 |
| `search_knowledge` | 图遍历检索 |

---

## 两种每日总结模式

### 模式1：AI主动调用（逐个群）
告诉AI "回顾一下今天" → AI调用 `daily_summary` → 生成当前群摘要

### 模式2：save_dream 全自动（所有群）
AI调用 `save_dream` → 系统自动处理**所有群** → 每个群各自摘要 → 跨群全局日报

---

## 配置分组

| 分组 | 配置项 |
|------|--------|
| 记忆功能 | 开关/做梦/日志/身份/事实提取 |
| **每日总结** | 注入开关/保留天数/**全局日报开关** |
| 记忆注入 | 条数/字符数/位置/刷新间隔 |
| 搜索与排序 | 结果数/RRF/MMR/标签数/TF-IDF |
| RAG混合记忆 | 衰减/稳定性/合并/阈值 |
| 内存与缓存 | 轻量/拼音/缓存 |
| 数据管理 | 清理/备份 |
| 网络与分类 | 地址/端口/模型/分类 |

---

## 安装

AstrBot管理面板搜索「记忆胶囊」。

```bash
pip install cachetools jieba
```

---

## 数据存储

```
AstrBot/data/plugin_data/memory_capsule/
├── memory.db    ← 所有数据（更新插件不丢失）
├── auth.json
└── memory_capsule_backups/
```

## 数据库表

| 表 | 用途 |
|---|------|
| `memories` | 长期记忆 |
| `relationships` | 人物关系 |
| `triples` | 知识图谱 |
| `conversation_logs` | 原始对话（7天） |
| `daily_summaries` | 单群每日摘要（30天） |
| `daily_global_digest` | 全局日报 |
| `dream_logs` | 梦境日志 |
| `auto_facts` | 自动提取事实 |
| `activities` | 操作记录 |
