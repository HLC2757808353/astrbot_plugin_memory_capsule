from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading
import asyncio

@register("memory_capsule", "引灯续昼", "记忆胶囊插件，用于存储和检索记忆", "v0.0.1", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
class MemoryCapsulePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.db_manager = None
        self.webui_server = None
        self.webui_thread = None
        self.config = config
        # 获取WebUI端口配置，默认为5000
        self.webui_port = config.get('webui_port', 5000) if config else 5000

    async def initialize(self):
        """插件初始化方法"""
        logger.info("记忆胶囊插件正在初始化...")
        
        # 创建必要的目录结构
        self._create_directories()
        
        # 初始化数据库管理
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config)
        self.db_manager.initialize()
        
        # 将实例注册到全局，供 __init__.py 调用
        from . import set_global_manager
        set_global_manager(self.db_manager)
        
        # 启动WebUI服务
        self._start_webui()
        
        logger.info("记忆胶囊插件初始化完成")

    def _create_directories(self):
        """创建必要的目录结构"""
        # 创建databases目录
        databases_dir = os.path.join(os.path.dirname(__file__), "databases")
        os.makedirs(databases_dir, exist_ok=True)
        
        # 创建webui目录
        webui_dir = os.path.join(os.path.dirname(__file__), "webui")
        os.makedirs(webui_dir, exist_ok=True)
        os.makedirs(os.path.join(webui_dir, "templates"), exist_ok=True)
        os.makedirs(os.path.join(webui_dir, "static"), exist_ok=True)
        
        # 创建data目录
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)

    def _start_webui(self):
        """启动WebUI服务"""
        try:
            # 检查端口是否被占用
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.webui_port))
            if result == 0:
                logger.warning(f"端口 {self.webui_port} 已被占用，尝试释放...")
                # 尝试释放端口（这里简化处理，实际可能需要更复杂的逻辑）
                sock.close()
            else:
                sock.close()
            
            from .webui.server import WebUIServer
            self.webui_server = WebUIServer(self.db_manager, port=self.webui_port)
            # 设置 server_thread 属性，确保 stop 方法能够正确识别线程
            self.webui_server.server_thread = threading.Thread(target=self.webui_server.run, daemon=True, name='WebUI Server')
            self.webui_server.server_thread.start()
            logger.info(f"WebUI服务已启动，端口: {self.webui_port}")
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")

    @filter.command("memory")
    async def memory_command(self, event: AstrMessageEvent):
        """记忆胶囊指令，用于测试和管理记忆"""
        user_name = event.get_sender_name()
        message_str = event.message_str
        
        # 简单的测试功能
        if "test" in message_str:
            yield event.plain_result(f"{user_name}, 记忆胶囊测试成功！")
        elif "status" in message_str:
            yield event.plain_result(f"{user_name}, 记忆胶囊运行正常，WebUI服务已启动")
        else:
            yield event.plain_result(f"{user_name}, 记忆胶囊命令格式：/memory test 或 /memory status")

    async def terminate(self):
        """插件销毁方法"""
        logger.info("记忆胶囊插件正在关闭...")
        
        # 关闭WebUI服务
        if self.webui_server:
            self.webui_server.stop()
        
        # 关闭数据库连接
        if self.db_manager:
            self.db_manager.close()
        
        logger.info("记忆胶囊插件已关闭")

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event, user_id, relation_type=None, summary_update=None, intimacy_change=-40, nickname=None, first_met_time=None, first_met_location=None, known_contexts=None):
        """
        更新对某人的印象或关系
        
        Args:
            user_id(str): 目标用户 ID
            relation_type(str): 新的关系定义
            summary_update(str): 新的印象总结 (会覆盖旧的)
            intimacy_change(int): 好感度变化值 (如 +5, -10)
            nickname(str): AI 对 TA 的称呼
            first_met_time(str): 初次见面时间
            first_met_location(str): 初次见面地点
            known_contexts(str): 遇到过的场景
            
        Returns:
            str: 更新结果
        """
        # 类型转换确保参数类型正确
        user_id = str(user_id)
        relation_type = str(relation_type) if relation_type is not None else None
        summary_update = str(summary_update) if summary_update is not None else None
        intimacy_change = int(intimacy_change)
        nickname = str(nickname) if nickname is not None else None
        first_met_time = str(first_met_time) if first_met_time is not None else None
        first_met_location = str(first_met_location) if first_met_location is not None else None
        known_contexts = str(known_contexts) if known_contexts is not None else None
        try:
            result = await asyncio.to_thread(self.db_manager.update_relationship, user_id, relation_type, None, summary_update, intimacy_change, nickname, first_met_time, first_met_location, known_contexts)
            logger.info(f"更新关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content, category="日常", tags="", target_user_id=None, source_platform="Web", source_context="", importance=5):
        """
        记下一个永久知识点
        
        Args:
            content(str): 要记住的内容
            category(str): 分类 (默认 "日常")
            tags(str): 标签 (逗号分隔)
            target_user_id(str): 如果是关于特定人的记忆，填这里
            source_platform(str): 来源 (默认 "Web")
            source_context(str): 场景
            importance(int): 重要性 (1-10，默认 5)
            
        Returns:
            str: 存储结果
        """
        # 类型转换确保参数类型正确
        content = str(content)
        category = str(category)
        tags = str(tags)
        target_user_id = str(target_user_id) if target_user_id is not None else None
        source_platform = str(source_platform)
        source_context = str(source_context)
        importance = int(importance)
        try:
            result = await asyncio.to_thread(self.db_manager.write_memory, content, category, tags, target_user_id, source_platform, source_context, importance)
            logger.info("存储记忆成功")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, target_user_id=None):
        """
        搜索过去的记忆
        
        Args:
            query(str): 搜索关键词或句子
            target_user_id(str): 限定搜索某人的相关记忆
            
        Returns:
            list: 搜索结果列表
        """
        # 类型转换确保参数类型正确
        query = str(query)
        target_user_id = str(target_user_id) if target_user_id is not None else None
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.search_memory, query, target_user_id)
            logger.info(f"搜索记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return []

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event, memory_id):
        """
        遗忘某条记忆
        
        Args:
            memory_id(int): 记忆的 ID (通常 AI 需要先搜到才能删)
            
        Returns:
            str: 删除结果
        """
        # 类型转换确保参数类型正确
        memory_id = int(memory_id)
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_memory, memory_id)
            logger.info(f"删除记忆成功: ID={memory_id}")
            return result
        except Exception as e:
            logger.error(f"删除记忆失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="get_all_memories")
    async def get_all_memories(self, event, limit=20):
        """
        获取所有记忆
        
        Args:
            limit(int): 限制数量，默认为20
            
        Returns:
            list: 记忆列表
        """
        # 类型转换确保参数类型正确
        limit = int(limit)
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_memories, limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return []

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event):
        """
        获取所有关系
        
        Returns:
            list: 关系列表
        """
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            logger.info(f"获取所有关系成功，找到 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return []

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event, user_id):
        """
        删除关系
        
        Args:
            user_id(str): 用户ID
            
        Returns:
            str: 删除结果
        """
        # 类型转换确保参数类型正确
        user_id = str(user_id)
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.delete_relationship, user_id)
            logger.info(f"删除关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="backup_database")
    async def backup_database(self, event):
        """
        备份数据库
        
        Returns:
            str: 备份结果
        """
        try:
            import asyncio
            result = await asyncio.to_thread(self.db_manager.backup)
            logger.info("数据库备份成功")
            return result
        except Exception as e:
            logger.error(f"数据库备份失败: {e}")
            return f"备份失败: {e}"

    @filter.on_llm_request()
    async def inject_relation_context(self, event: AstrMessageEvent):
        """
        注入用户关系信息到AI上下文
        
        每次对话时，自动获取用户的关系信息并注入到系统提示词中
        无关系时提醒LLM
        """
        try:
            # 获取用户信息
            user_id = event.get_sender_id()
            
            # 查找用户关系信息
            import asyncio
            relationships = await asyncio.to_thread(self.db_manager.get_all_relationships)
            user_relation = None
            
            for relation in relationships:
                if relation['user_id'] == user_id:
                    user_relation = relation
                    break
            
            # 构建关系信息上下文
            if user_relation:
                # 确保所有字段都有值
                nickname = user_relation['nickname'] or '未知'
                first_met_time = user_relation['first_met_time'] or '未知'
                first_met_location = user_relation['first_met_location'] or '未知'
                known_contexts = user_relation['known_contexts'] or '未知'
                relation_type = user_relation['relation_type'] or '未知'
                summary = user_relation['summary'] or '无'
                
                # 构建关系信息格式
                relation_context = f"\n\n<Relationship> 当前关系状态：\n- 用户ID: {user_relation['user_id']}\n- 昵称: {nickname}\n- 关系类型: {relation_type}\n- 好感度: {user_relation['intimacy']}\n- 初次见面时间: {first_met_time}\n- 初次见面地点: {first_met_location}\n- 认识群组: {known_contexts}\n- 核心印象: {summary}\n</Relationship>\n"
            else:
                # 如果查询为空，返回指定格式
                relation_context = f"\n\n<Relationship>当前对象未被记录在关系图谱里</Relationship>\n"
                logger.info(f"用户 {user_id} 暂无关系信息")
            
            # 检查配置，确定注入位置
            inject_to = self.config.get('context_inject_position', 'system')
            if inject_to == 'system':
                # 注入到系统提示词
                if hasattr(event, 'system_prompt'):
                    event.system_prompt += relation_context
                elif hasattr(event, 'context') and hasattr(event.context, 'system_prompt'):
                    event.context.system_prompt = (event.context.system_prompt or "") + relation_context
            else:
                # 注入到用户上下文
                event.message_str = relation_context + '\n' + event.message_str
            
            logger.info(f"成功注入用户 {user_id} 的关系信息到上下文")
        except Exception as e:
            logger.error(f"注入关系信息失败: {e}")
        return event

# 外部接口，供其他插件调用
# 注意：这些函数已在 __init__.py 中重新定义，使用单例模式
