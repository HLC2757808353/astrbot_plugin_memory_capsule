from typing import Dict, Any
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import ProviderRequest
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
        # 关系注入缓存
        self.relation_injection_cache = {}
        # 关系注入刷新时间（默认1小时）
        self.relation_injection_refresh_time = config.get('relation_injection_refresh_time', 3600) if config else 3600

    async def initialize(self):
        """插件初始化方法"""
        logger.info("记忆胶囊插件正在初始化...")
        
        # 检查依赖
        self._check_dependencies()
        
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
    
    def _check_dependencies(self):
        """检查插件所需的依赖"""
        logger.info("检查插件依赖...")
        
        # 检查必要依赖
        required_dependencies = {
            'jieba': '分词库，用于智能标签提取',
            'pypinyin': '拼音库，用于拼音匹配'
        }
        
        for dep_name, dep_desc in required_dependencies.items():
            try:
                __import__(dep_name)
                logger.info(f"✓ 依赖 {dep_name} 已安装")
            except ImportError:
                logger.warning(f"⚠ 依赖 {dep_name} 未安装 - {dep_desc}")
                logger.warning(f"  建议运行: pip install {dep_name}")
        
        # 检查可选依赖
        optional_dependencies = {
            'python-Levenshtein': '字符串相似度计算',
            'msgpack': '缓存序列化'
        }
        
        for dep_name, dep_desc in optional_dependencies.items():
            try:
                __import__(dep_name.replace('-', '_'))
                logger.info(f"✓ 可选依赖 {dep_name} 已安装")
            except ImportError:
                logger.info(f"ℹ 可选依赖 {dep_name} 未安装 - {dep_desc}")

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
            # 检查并尝试释放端口
            import socket
            import subprocess
            import time
            
            # 检查端口是否被占用
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.webui_port))
            port_available = result != 0
            
            if not port_available:
                logger.warning(f"端口 {self.webui_port} 已被占用，尝试释放...")
                # 尝试找到并终止占用端口的进程
                try:
                    # 使用netstat命令查找占用端口的进程
                    netstat_result = subprocess.run(
                        f'netstat -ano | findstr :{self.webui_port}',
                        shell=True,
                        capture_output=True,
                        text=True
                    )
                    lines = netstat_result.stdout.strip().split('\n')
                    pids = set()
                    for line in lines:
                        if line:
                            parts = line.split()
                            if len(parts) >= 5:
                                pid = parts[4]
                                pids.add(pid)
                    
                    # 终止找到的进程
                    for pid in pids:
                        if pid != '0':  # 排除TIME_WAIT状态的连接
                            logger.info(f"发现占用端口 {self.webui_port} 的进程 PID: {pid}")
                            try:
                                # 使用taskkill命令终止进程
                                kill_result = subprocess.run(
                                    f'taskkill /PID {pid} /F',
                                    shell=True,
                                    capture_output=True,
                                    text=True
                                )
                                if kill_result.returncode == 0:
                                    logger.info(f"已成功终止进程 {pid}")
                                else:
                                    logger.error(f"终止进程 {pid} 失败: {kill_result.stderr}")
                            except Exception as kill_e:
                                logger.error(f"终止进程 {pid} 失败: {kill_e}")
                    
                    # 等待一段时间后再次检查端口是否可用
                    time.sleep(1)
                    result = sock.connect_ex(('localhost', self.webui_port))
                    port_available = result != 0
                    if not port_available:
                        logger.error(f"端口 {self.webui_port} 仍然被占用，无法启动WebUI服务")
                        sock.close()
                        return
                except Exception as e:
                    logger.error(f"释放端口失败: {e}")
                    sock.close()
                    return
            sock.close()
            
            # 再次检查端口是否可用
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', self.webui_port))
            port_available = result != 0
            sock.close()
            
            if not port_available:
                logger.error(f"端口 {self.webui_port} 仍然被占用，无法启动WebUI服务")
                return
            
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
    async def update_relationship(self, event, user_id, relation_type=None, summary_update=None, intimacy_change=-40, nickname=None, first_met_location=None, known_contexts=None):
        """
        更新对某人的印象或关系
        
        Args:
            user_id(str): 目标用户 ID
            relation_type(str): 新的关系定义
            summary_update(str): 新的印象总结 (会覆盖旧的)
            intimacy_change(int): 好感度变化值 (如 +5, -10)
            nickname(str): AI 对 TA 的称呼
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
        first_met_location = str(first_met_location) if first_met_location is not None else None
        known_contexts = str(known_contexts) if known_contexts is not None else None
        try:
            result = await asyncio.to_thread(self.db_manager.update_relationship, user_id, relation_type, summary_update, intimacy_change, nickname, first_met_location, known_contexts)
            logger.info(f"更新关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content, category=None, tags=""):
        """
        记下一个永久知识点
        
        Args:
            content(str): 要记住的内容
            category(str): 分类 (AI指定)
            tags(str): 标签 (逗号分隔)
            
        Returns:
            str: 存储结果
        """
        # 检查记忆宫殿是否启用
        if not self.config.get('memory_palace', True):
            return "记忆宫殿模块已禁用"
            
        # 类型转换确保参数类型正确
        content = str(content)
        if category is not None:
            category = str(category)
        tags = str(tags)
        try:
            result = await asyncio.to_thread(self.db_manager.write_memory, content, category, tags)
            logger.info("存储记忆成功")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=5):
        """
        搜索过去的记忆
        
        Args:
            query(str): 搜索关键词或句子
            category_filter(str): 分类过滤
            limit(int): 返回结果数量限制
            
        Returns:
            list: 搜索结果列表
        """
        # 检查记忆宫殿是否启用
        if not self.config.get('memory_palace', True):
            return []
            
        # 类型转换确保参数类型正确
        query = str(query)
        category_filter = str(category_filter) if category_filter is not None else None
        limit = int(limit)
        try:
            import asyncio
            results = await asyncio.to_thread(self.db_manager.search_memory, query, category_filter, limit)
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
        # 检查记忆宫殿是否启用
        if not self.config.get('memory_palace', True):
            return "记忆宫殿模块已禁用"
            
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
        # 检查记忆宫殿是否启用
        if not self.config.get('memory_palace', True):
            return []
            
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

    @filter.llm_tool(name="self_optimize")
    async def self_optimize(self, event):
        """
        执行自我优化
        
        Returns:
            str: 优化结果
        """
        try:
            import asyncio
            await asyncio.to_thread(self.db_manager.self_optimize)
            logger.info("自我优化执行成功")
            return "自我优化执行成功"
        except Exception as e:
            logger.error(f"自我优化执行失败: {e}")
            return f"优化失败: {e}"

    @filter.llm_tool(name="update_search_weights")
    async def update_search_weights(self, event, weights: dict):
        """
        更新搜索权重配置
        
        Args:
            weights(dict): 权重参数，如 tag_match, recent_boost 等
            
        Returns:
            str: 更新结果
        """
        try:
            import asyncio
            await asyncio.to_thread(self.db_manager.update_search_weights, **weights)
            logger.info("搜索权重更新成功")
            return "搜索权重更新成功"
        except Exception as e:
            logger.error(f"更新搜索权重失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="update_search_strategy")
    async def update_search_strategy(self, event, strategy: dict):
        """
        更新搜索策略配置
        
        Args:
            strategy(dict): 策略参数，如 match_type, synonym_expansion 等
            
        Returns:
            str: 更新结果
        """
        try:
            import asyncio
            await asyncio.to_thread(self.db_manager.update_search_strategy, **strategy)
            logger.info("搜索策略更新成功")
            return "搜索策略更新成功"
        except Exception as e:
            logger.error(f"更新搜索策略失败: {e}")
            return f"更新失败: {e}"

    @filter.on_llm_request()
    async def inject_relation_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        注入用户关系信息到AI上下文
        
        每次对话时，自动获取用户的关系信息并注入到系统提示词中
        无关系时提醒LLM
        """
        try:
            # 获取用户信息
            user_id = event.get_sender_id()
            
            # 检查缓存
            import time
            current_time = time.time()
            cache_key = f"relation_injection_{user_id}"
            
            if cache_key in self.relation_injection_cache:
                last_injection_time = self.relation_injection_cache[cache_key]
                if current_time - last_injection_time < self.relation_injection_refresh_time:
                    # 缓存未过期，跳过注入
                    logger.info(f"用户 {user_id} 的关系信息在缓存期内，跳过注入")
                    return req
            
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
                first_met_location = user_relation['first_met_location'] or '未知'
                known_contexts = user_relation['known_contexts'] or '未知'
                relation_type = user_relation['relation_type'] or '未知'
                summary = user_relation['summary'] or '无'
                
                # 构建关系信息格式
                relation_context = f"\n\n<Relationship> 当前关系状态：\n- 用户ID: {user_relation['user_id']}\n- 昵称: {nickname}\n- 关系类型: {relation_type}\n- 好感度: {user_relation['intimacy']}\n- 初次见面地点: {first_met_location}\n- 多次相遇群组: {known_contexts}\n- 核心印象: {summary}\n</Relationship>\n"
            else:
                # 如果查询为空，返回指定格式
                relation_context = f"\n\n<Relationship>当前对象未被记录在关系图谱里</Relationship>\n"
                logger.info(f"用户 {user_id} 暂无关系信息")
            
            # 检查配置，确定注入方式
            injection_method = self.config.get('context_inject_position', 'user_prompt')
            
            if injection_method == 'system_prompt':
                # 注入到系统提示词
                req.system_prompt = (req.system_prompt or "") + relation_context
                logger.info(f"成功注入用户 {user_id} 的关系信息到系统提示词")
            elif injection_method == 'user_prompt':
                # 注入到用户消息
                req.prompt = relation_context + '\n' + (req.prompt or "")
                logger.info(f"成功注入用户 {user_id} 的关系信息到用户提示词")
            elif injection_method == 'insert_system_prompt':
                # 向上下文列表中添加一条新的系统消息
                if hasattr(req, 'messages'):
                    # 检查messages是否存在
                    req.messages.insert(0, {
                        'role': 'system',
                        'content': relation_context
                    })
                    logger.info(f"成功向上下文列表添加用户 {user_id} 的关系信息系统消息")
                else:
                    # 如果messages不存在，默认注入到系统提示词
                    req.system_prompt = (req.system_prompt or "") + relation_context
                    logger.info(f"成功注入用户 {user_id} 的关系信息到系统提示词")
            else:
                # 默认注入到用户消息
                req.prompt = relation_context + '\n' + (req.prompt or "")
                logger.info(f"成功注入用户 {user_id} 的关系信息到用户提示词")
            
            # 更新缓存
            self.relation_injection_cache[cache_key] = current_time
            
        except Exception as e:
            logger.error(f"注入关系信息失败: {e}")
        return event

# 外部接口，供其他插件调用
# 注意：这些函数已在 __init__.py 中重新定义，使用单例模式
