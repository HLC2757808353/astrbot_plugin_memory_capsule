# 记忆胶囊 🧠

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-v0.9.5-red.svg)]()

*为 AI 赋予持久记忆与智能关系管理能力*

</div>

---

## ✨ 这是什么？

**记忆胶囊**是一个功能丰富的 AstrBot 插件，让 AI 拥有"长期记忆"。它不仅能记住知识，还能记住人。

想象一下：AI 能记得你上次聊了什么、你的喜好、你们的约定...就像一个真正的朋友。💬

---

## 🎯 核心功能

### 📚 记忆宫殿
- **智能存储** - 自动提取标签、分类、评估重要性
- **多维搜索** - 关键词 + 标签 + 分类 + 时间范围 + 拼音模糊匹配
- **FTS5全文索引** - 毫秒级快速检索
- **MMR多样性筛选** - 避免返回过多相似结果

### 🔗 关系图谱
- **人物档案** - 昵称、关系类型、印象总结、初次见面地点
- **身份映射** - 自动识别同一用户在不同群组的身份（跨群关联）
- **自动注入** - 对话时自动将用户信息注入AI上下文
- **智能缓存** - 用户切换立即刷新，同用户按时间隔更新

### 🎨 WebUI 管理面板
- **可视化操作** - 浏览、搜索、编辑记忆和关系
- **密码保护** - 首次启动生成临时Token，可自定义登录密码
- **数据管理** - 一键备份/恢复数据库
- **系统配置** - 调整搜索权重、清理策略等

### 💾 数据安全
- **阶梯式备份** - 小时/天/周/月四级备份策略
- **自动清理** - 按时间或数量自动清理旧记忆
- **数据迁移** - 自动检测并升级旧版数据库结构

---

## 🚀 快速开始

### 安装插件

```bash
# 方式1: Git克隆
git clone https://github.com/HLC2757808353/astrbot_plugin_memory_capsule.git
# 复制到 AstrBot 插件目录
cp -r astrbot_plugin_memory_capsule <AstrBot路径>/data/plugins/

# 方式2: 插件市场安装
# 在 AstrBot WebUI 搜索「记忆胶囊」并安装
```

### 安装依赖

```bash
pip install jieba pypinyin

# 可选（提升体验）
pip install python-Levenshtein msgpack
```

### 启动后...

1. 查看 AstrBot 日志，找到 WebUI 地址和临时密码
2. 打开浏览器访问 `http://localhost:5000`（默认端口）
3. 输入临时密码首次登录
4. 在设置页面修改为自定义密码 ✅

> **提示**：WebUI端口可在配置中修改，默认5000

---

## 🤖 AI 如何使用？

插件会自动为 AI 注册以下工具，AI 可以自主决定何时使用：

| 工具 | 用途 |
|------|------|
| `write_memory` | "这个知识点很重要，我记下来" |
| `search_memory` | "用户问的事情我之前记过吗？" |
| `delete_memory` | "这条记忆过时了，删掉吧" |
| `update_relationship` | "原来TA喜欢Python啊，记录一下" |
| `search_relationship` | "这个人是谁来着？查一下" |

**无需手动触发** - AI 会根据对话内容智能判断是否需要调用这些工具！

---

## ⚙️ 配置说明

在 `_conf_schema.json` 或 WebUI 中可以调整：

### 常用配置项

```json
{
  "webui_port": 5000,                    // WebUI端口号
  "memory_palace": true,                 // 启用记忆宫殿
  "memory_categories": [                 // 自定义分类
    "技术笔记", "生活记录", "学习资料", "个人想法"
  ],
  "context_inject_position": "user_prompt", // 关系信息注入位置
  "relation_injection_refresh_time": 3600,   // 注入刷新间隔(秒)
  "backup_interval": 24,                  // 备份间隔(小时)
  "cache_ttl": 300                       // 缓存过期时间(秒)
}
```

### 高级配置

- **搜索权重** - 调整标签匹配、时间衰减、重要性等权重
- **搜索策略** - AND/OR模式、同义词扩展、分类过滤
- **MMR参数** - 平衡相关性和结果多样性 (0-1)
- **清理策略** - unaccessed(未访问) / oldest(最旧) / random(随机)

详细配置说明请查看 [`_conf_schema.json`](_conf_schema.json)

---

## 📁 文件结构

```
astrbot_plugin_memory_capsule/
├── main.py                 # 主插件逻辑
├── __init__.py             # 外部接口
├── metadata.yaml           # 插件元信息
├── _conf_schema.json       # 配置 schema
├── databases/
│   ├── db_manager.py       # 数据库管理核心
│   └── backup.py           # 备份管理
├── webui/
│   ├── server.py           # Flask Web服务
│   └── templates/          # HTML模板
└── data/
    ├── memory.db           # SQLite数据库
    └── backups/            # 备份文件目录
        ├── auth.json       # 登录认证信息（自动生成）
        └── *.db            # 备份文件
```

---

## 🔐 安全性说明

### WebUI 认证机制

**首次使用流程：**
1. 启动时自动生成随机临时Token（显示在日志中）
2. 使用临时Token首次登录WebUI
3. 强制要求设置自定义密码
4. 之后使用自定义密码登录

**安全特性：**
- ✅ 密码哈希存储（不存明文）
- ✅ Session会话管理
- ✅ 每个实例独立认证（拷贝插件不影响原实例）
- ✅ 插件更新不丢失密码（密码存在data目录）

**关于多实例部署：**
每个 AstrBot 实例都有独立的 `data/auth.json` 文件，互不影响。即使别人复制你的插件代码，也会有自己独立的认证系统。

---

## 🛠️ 故障排除

### 常见问题

<details>
<summary><b>❌ WebUI 无法访问</b></summary>

1. 检查端口占用：`netstat -ano | findstr 5000`
2. 查看日志中的错误信息
3. 尝试修改 `webui_port` 配置
</details>

<details>
<summary><b>❌ 忘记 WebUI 密码</b></summary>

删除 `data/auth.json` 文件，重启插件即可重新生成临时Token
</details>

<details>
<summary><b>❌ 搜索结果不准确</b></summary>

1. 调整搜索权重配置
2. 开启同义词扩展
3. 增加 `max_extracted_tags` 数量
</details>

<details>
<summary><b>❌ 依赖安装失败</b></summary>

```bash
# 使用国内镜像源加速
pip install jieba pypinyin -i https://pypi.tuna.tsinghua.edu.cn/simple
```
</details>

---

## 📊 版本历史

### v0.9.5 (当前版本)
- ✨ 新增 WebUI 密码认证系统
- ✨ 新增身份映射系统（跨群身份识别）
- ✨ 新增 MMR 多样性搜索算法
- ✨ 新增 FTS5 全文搜索引擎
- ✨ 新增阶梯式备份策略
- ✨ 新增 TTL 缓存机制
- 🐛 修复多个已知问题
- ⚡ 性能优化和代码重构

### v0.7.x
- 基础记忆存储和检索
- 关系图谱功能
- WebUI 管理界面
- 自动上下文注入

---

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！

- 🐛 发现Bug？ → [提交Issue](https://github.com/HLC2757808353/astrbot_plugin_memory_capsule/issues)
- 💡 有新想法？ → 发起讨论或直接PR
- 📝 改进文档？ → 欢迎完善README和注释

---

## 📄 许可证

[MIT License](LICENSE)

---

## 👤 作者

**引灯续昼**

- GitHub: [@HLC2757808353](https://github.com/HLC2757808353)
- 项目地址: [astrbot_plugin_memory_capsule](https://github.com/HLC2757808353/astrbot_plugin_memory_capsule)

---

## 🙏 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 强大的聊天机器人框架
- [jieba](https://github.com/fxsjy/jieba) - 中文分词库
- [pypinyin](https://github.com/mozillazg/python-pinyin) - 拼音转换库

---

<div align="center">

**如果觉得有用，给个 ⭐ 吧！**

*Made with ❤️ by 引灯续昼*

</div>
