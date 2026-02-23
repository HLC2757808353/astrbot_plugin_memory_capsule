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

### 4.1 存储数据

其他插件可以通过以下接口存储数据：

```python
from astrbot_plugin_memory_capsule import store_plugin_data

# 存储数据
result = store_plugin_data(
    plugin_name="your_plugin_name",  # 插件名称
    data_type="data_type",           # 数据类型
    content="数据内容",              # 具体内容
    metadata={"key": "value"}       # 附加元数据（可选）
)
print(result)  # 返回存储结果
```

### 4.2 查询数据

```python
from astrbot_plugin_memory_capsule import query_plugin_data

# 查询数据
results = query_plugin_data(
    query_keyword="关键词",          # 搜索关键词（可选）
    plugin_name="your_plugin_name",  # 插件名称（可选）
    data_type="data_type"           # 数据类型（可选）
)

for item in results:
    print(f"{item['plugin_name']}/{item['data_type']}: {item['content']}")
```

### 4.3 获取记忆管理器

```python
from astrbot_plugin_memory_capsule import get_memory_manager

# 获取记忆管理器实例
db_manager = get_memory_manager()

# 使用管理器的其他方法
all_data = db_manager.get_all_plugin_data()
all_relations = db_manager.get_all_relations()
backup_result = db_manager.backup()
```

## 五、数据存储结构

### 5.1 插件数据表 (`plugin_data`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| id | INTEGER | 主键，自增 |
| plugin_name | TEXT | 插件名称 |
| data_type | TEXT | 数据类型 |
| content | TEXT | 数据内容 |
| metadata | TEXT | 元数据（JSON格式） |
| category | TEXT | 分类路径 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

### 5.2 关系数据表 (`relations`)

| 字段名 | 类型 | 描述 |
|--------|------|------|
| id | INTEGER | 主键，自增 |
| user_id | TEXT | 用户ID |
| group_id | TEXT | 群组ID |
| nickname | TEXT | 昵称 |
| alias_history | TEXT | 昵称历史 |
| impression_summary | TEXT | 印象总结 |
| favor_level | INTEGER | 好感度(0-100) |
| interaction_count | INTEGER | 互动次数 |
| last_interaction_time | TIMESTAMP | 最后互动时间 |
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
from astrbot_plugin_memory_capsule import store_plugin_data

def process_video(video_url):
    # 处理视频，生成观后感
    summary = generate_summary(video_url)
    
    # 存储到记忆胶囊
    result = store_plugin_data(
        plugin_name="bilibili_watcher",
        data_type="video_summary",
        content=summary,
        metadata={"video_url": video_url, "title": get_video_title(video_url)}
    )
    
    return f"视频处理完成，{result}"
```

### 7.2 小说阅读插件示例

```python
from astrbot_plugin_memory_capsule import store_plugin_data

def read_chapter(novel_name, chapter):
    # 读取章节，生成笔记
    notes = generate_notes(novel_name, chapter)
    
    # 存储到记忆胶囊
    result = store_plugin_data(
        plugin_name="novel_reader",
        data_type="chapter_notes",
        content=notes,
        metadata={"novel_name": novel_name, "chapter": chapter}
    )
    
    return f"章节阅读完成，{result}"
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
├── databases/              # 数据库管理
│   ├── __init__.py
│   ├── db_manager.py       # 数据库核心操作
│   └── backup.py           # 备份功能
├── webui/                  # WebUI 管理
│   ├── __init__.py
│   ├── server.py           # Flask 服务器
│   └── templates/          # HTML 模板
├── data/                   # 数据存储
│   ├── memory.db           # SQLite 数据库
│   └── backups/            # 备份文件
└── README.md               # 使用文档
```

## 十一、许可证

本插件采用 MIT 许可证，详见 LICENSE 文件。
