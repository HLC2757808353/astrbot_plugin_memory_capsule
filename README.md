# AstrBot 记忆胶囊插件

## 一、插件介绍

记忆胶囊是 AstrBot 的一个核心插件，用于**存储和管理所有插件产生的记忆数据**，包括视频观后感、小说阅读笔记、关系互动记录等。

### 核心功能

- **永久存储**：将重要信息永久存储到本地 SQLite 数据库
- **智能分类**：自动按插件名称、数据类型、时间等维度分类
- **关系管理**：记录和管理与用户的关系，包括好感度和印象
- **多插件支持**：提供统一接口供其他插件存储数据
- **WebUI管理**：提供可视化界面查看和管理记忆数据
- **自动备份**：定期自动备份数据库，防止数据丢失
- **记忆宫殿**：智能记忆管理系统，支持标签提取、同义词扩展、智能搜索
- **记忆宫殿开关**：可在配置中启用或禁用记忆宫殿模块
- **数据迁移**：自动将旧数据迁移到新的表结构
- **智能搜索**：支持关键词搜索、分类过滤、热度排序

## 二、安装方法

### 2.1 自动安装

在 AstrBot 插件管理界面中搜索 "记忆胶囊" 并点击安装。

### 2.2 手动安装

1. 下载本插件的压缩包
2. 解压到 AstrBot 的插件目录 `data/plugins/` 下
3. 重启 AstrBot

## 三、使用方法

### 3.1 用户命令

在聊天中发送以下命令：

- `/memory test` - 测试记忆胶囊是否正常运行
- `/memory status` - 查看记忆胶囊的运行状态

### 3.2 WebUI 管理

插件启动后，WebUI 服务会自动运行，访问以下地址：

- **地址**：http://localhost:5000
- **功能**：
  - 查看系统状态和存储统计
  - 管理笔记数据（添加、删除）
  - 查看关系记录和好感度

## 四、插件开发接口

### 4.1 存储记忆

其他插件可以通过以下接口存储记忆：

```python
from astrbot_plugin_memory_capsule import get_memory_manager

# 获取记忆管理器实例
db_manager = get_memory_manager()

# 存储记忆
result = db_manager.write_memory(
    content="记忆内容",              # 记忆正文
    category="日常",                # 分类（默认 "日常"）
    tags="标签1,标签2",            # 标签（逗号分隔）
    target_user_id="用户ID",         # 如果是关于特定人的记忆，填这里
    source_platform="Web",         # 来源（默认 "Web"）
    source_context="场景",          # 场景
    importance=5                    # 重要性（1-10，默认 5）
)
print(result)  # 返回存储结果
```

### 4.2 搜索记忆

```python
from astrbot_plugin_memory_capsule import get_memory_manager

# 获取记忆管理器实例
db_manager = get_memory_manager()

# 搜索记忆
results = db_manager.search_memory(
    query="搜索关键词",             # 搜索关键词或句子
    target_user_id="用户ID"         # 限定搜索某人的相关记忆（可选）
)

for item in results:
    print(f"{item['category']}: {item['content']}")
```

### 4.3 管理关系

```python
from astrbot_plugin_memory_capsule import get_memory_manager

# 获取记忆管理器实例
db_manager = get_memory_manager()

# 更新关系
result = db_manager.update_relationship(
    user_id="用户ID",               # 目标用户 ID
    relation_type="朋友",          # 新的关系定义
    summary_update="核心印象",      # 新的印象总结
    intimacy_change=5,             # 好感度变化值（如 +5, -10）
    nickname="昵称",               # AI 对 TA 的称呼
    first_met_time="2026-01-01 12:00:00",  # 初次见面时间
    first_met_location="QQ群:12345",  # 初次见面地点
    known_contexts="QQ群:12345"     # 遇到过的场景
)
print(result)  # 返回更新结果
```

### 4.4 其他方法

```python
from astrbot_plugin_memory_capsule import get_memory_manager

# 获取记忆管理器实例
db_manager = get_memory_manager()

# 获取所有记忆
all_memories = db_manager.get_all_memories(limit=20)

# 获取所有关系
all_relationships = db_manager.get_all_relationships()

# 删除记忆
delete_result = db_manager.delete_memory(memory_id)

# 删除关系
delete_relation_result = db_manager.delete_relationship(user_id)

# 备份数据库
backup_result = db_manager.backup()

# 从备份恢复
restore_result = db_manager.restore_from_backup(backup_filename)

# 获取最近活动
activities = db_manager.get_recent_activities(limit=10)
```

## 五、数据存储结构

### 5.1 记忆表 (`memories`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| id | INTEGER | 主键，自增 |
| user_id | TEXT | 关联对象（如果是关于某人的记忆） |
| source_platform | TEXT | 来源（QQ, Bilibili, Web） |
| source_context | TEXT | 场景（群号, 视频ID） |
| category | TEXT | 分类（社交, 知识, 娱乐, 日记） |
| tags | TEXT | 标签（方便检索） |
| content | TEXT | 记忆正文 |
| importance | INTEGER | 重要性（1-10） |
| access_count | INTEGER | 被搜索到的次数（用于热度统计） |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

### 5.2 关系表 (`relationships`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| user_id | TEXT | 对方 QQ 号（主键） |
| nickname | TEXT | AI 对 TA 的称呼 |
| relation_type | TEXT | 关系（如: 朋友, 损友） |
| intimacy | INTEGER | 好感度（0-100） |
| tags | TEXT | 印象标签（如: "幽默,程序员"） |
| summary | TEXT | 核心印象（覆盖式更新，不追加） |
| first_met_time | TIMESTAMP | 初次见面时间 |
| first_met_location | TEXT | 初次见面地点（如: "QQ群:12345"） |
| known_contexts | TEXT | 遇到过的场景（JSON列表） |
| updated_at | TIMESTAMP | 更新时间 |

### 5.3 标签表 (`tags`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| memory_id | INTEGER | 关联哪条记忆 |
| tag | TEXT | 标签内容 |
| source | TEXT | 来源：auto=自动，manual=手动 |
| created_at | TIMESTAMP | 创建时间 |

### 5.4 同义词表 (`synonyms`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| word | TEXT | 基础词（总是较小的词） |
| synonym | TEXT | 同义词（总是较大的词） |
| source | TEXT | 来源：rule=规则，learned=学习 |
| strength | FLOAT | 同义强度（0.0-1.0） |

### 5.5 活动记录表 (`activities`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| id | INTEGER | 主键，自增 |
| action | TEXT | 操作类型（添加记忆, 更新关系, 删除记忆等） |
| details | TEXT | 操作详情 |
| created_at | TIMESTAMP | 创建时间 |

## 六、备份与恢复

### 6.1 自动备份

- **频率**：每 24 小时自动备份一次
- **位置**：`data/backups/` 目录
- **保留**：最多保留 10 个最新备份

### 6.2 手动备份

通过 WebUI 或 API 执行手动备份：

```python
db_manager = get_memory_manager()
result = db_manager.backup()
print(result)
```

### 6.3 从备份恢复

```python
db_manager = get_memory_manager()
result = db_manager.restore_from_backup("memory_20260223_000000.db")
print(result)
```

## 七、示例代码

### 7.1 刷视频插件示例

```python
from astrbot_plugin_memory_capsule import get_memory_manager

def process_video(video_url):
    # 处理视频，生成观后感
    summary = generate_summary(video_url)
    
    # 获取记忆管理器实例
    db_manager = get_memory_manager()
    
    # 存储到记忆胶囊
    result = db_manager.write_memory(
        content=summary,
        category="娱乐",
        tags="视频,观后感",
        source_platform="Bilibili",
        source_context=video_url,
        importance=7
    )
    
    return f"视频处理完成，{result}"
```

### 7.2 小说阅读插件示例

```python
from astrbot_plugin_memory_capsule import get_memory_manager

def read_chapter(novel_name, chapter):
    # 读取章节，生成笔记
    notes = generate_notes(novel_name, chapter)
    
    # 获取记忆管理器实例
    db_manager = get_memory_manager()
    
    # 存储到记忆胶囊
    result = db_manager.write_memory(
        content=notes,
        category="知识",
        tags="小说,阅读笔记",
        source_platform="Web",
        source_context=f"{novel_name}:第{chapter}章",
        importance=6
    )
    
    return f"章节阅读完成，{result}"
```

### 7.3 关系管理示例

```python
from astrbot_plugin_memory_capsule import get_memory_manager

def update_user_relation(user_id, message):
    # 分析消息，提取关系信息
    relation_type = analyze_relation_type(message)
    summary = generate_impression_summary(message)
    intimacy_change = calculate_intimacy_change(message)
    
    # 获取记忆管理器实例
    db_manager = get_memory_manager()
    
    # 更新关系
    result = db_manager.update_relationship(
        user_id=user_id,
        relation_type=relation_type,
        summary_update=summary,
        intimacy_change=intimacy_change
    )
    
    return f"关系更新完成，{result}"
```

## 八、常见问题

### 8.1 WebUI 无法访问

- 检查插件是否正常启动
- 检查端口 5000 是否被占用
- 尝试重启 AstrBot

### 8.2 数据存储失败

- 检查插件目录是否有写入权限
- 检查磁盘空间是否充足
- 查看 AstrBot 日志获取详细错误信息

### 8.3 备份失败

- 检查 `data/backups/` 目录是否存在且有写入权限
- 检查磁盘空间是否充足

## 九、版本历史

- **v0.4.1** - 记忆宫殿优化版
  - 实现记忆宫殿模块，支持智能标签提取和同义词扩展
  - 添加记忆宫殿开关功能，可在配置中启用或禁用
  - 优化数据库结构，添加 tags 和 synonyms 表
  - 实现数据迁移功能，自动将旧数据迁移到新表结构
  - 支持智能搜索，包括关键词搜索、分类过滤、热度排序
  - 优化关系信息注入格式，使用 <Relationship> 标签
  - 修复多个 bug，提高系统稳定性

- **v0.0.1** - 初始版本
  - 实现基本的存储和查询功能
  - 添加 WebUI 管理界面
  - 实现自动备份功能

## 十、开发与贡献

欢迎提交 Issue 和 Pull Request 来改进本插件！

### 开发环境

- Python 3.8+
- 依赖：Flask（WebUI）

### 目录结构

```
astrbot_plugin_memory_capsule/
├── main.py                 # 插件入口
├── __init__.py             # 模块初始化
├── _conf_schema.json       # 配置文件
├── databases/              # 数据库管理
│   ├── __init__.py
│   ├── db_manager.py       # 数据库核心操作
│   └── backup.py           # 备份功能
├── webui/                  # WebUI 管理
│   ├── __init__.py
│   ├── server.py           # Flask 服务器
│   └── templates/          # HTML 模板
│       ├── index.html      # 仪表盘
│       ├── memories.html   # 记忆宫殿
│       ├── relationships.html # 关系图谱
│       ├── settings.html   # 系统设置
│       └── notes.html      # 笔记页面
├── data/                   # 数据存储
│   ├── memory.db           # SQLite 数据库
│   └── backups/            # 备份文件
└── README.md               # 使用文档
```

## 十一、许可证

本插件采用 MIT 许可证，详见 LICENSE 文件。
