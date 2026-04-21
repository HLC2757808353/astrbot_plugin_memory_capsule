from typing import Dict, Any
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import os
import threading
import asyncio
import json

@register("memory_capsule", "引灯续昼", "记忆胶囊插件，用于存储和检索记忆", "v0.9.5", "https://github.com/HLC2757808353/astrbot_plugin_memory_capsule")
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
        # 上次对话的用户ID（用于检测用户切换）
        self.last_relation_user_id = None
        # 关系注入刷新时间（默认1小时）
        self.relation_injection_refresh_time = config.get('relation_injection_refresh_time', 3600) if config else 3600
        logger.info(f"关系注入刷新时间配置: {self.relation_injection_refresh_time}秒")

    async def initialize(self):
        """插件初始化方法"""
        logger.info("记忆胶囊插件正在初始化...")
        
        # 检查依赖
        self._check_dependencies()
        
        # 创建必要的目录结构
        self._create_directories()
        
        # 初始化数据库管理
        from .databases.db_manager import DatabaseManager
        self.db_manager = DatabaseManager(self.config, self.context)
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
        
        # 🎯 关键改进：先生成Token和认证信息（无论端口是否被占用）
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        
        try:
            from .webui.auth import AuthManager
            auth_manager = AuthManager(data_dir)
            logger.info(f"✅ 认证信息已生成（Token见上方日志）")
        except Exception as e:
            logger.error(f"生成认证信息失败: {e}")
            auth_manager = None
        
        # 然后检查端口并启动服务器
        try:
            import socket
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', self.webui_port))
            sock.close()
            
            if result == 0:
                # 端口被占用 - 尝试自动清理旧进程
                logger.warning(f"⚠️  端口 {self.webui_port} 已被占用，尝试自动释放...")
                
                if self._force_release_port(self.webui_port):
                    logger.info(f"✅ 端口 {self.webui_port} 已成功释放")
                else:
                    logger.error(f"❌ 端口 {self.webui_port} 无法自动释放")
                    logger.error(f"   请手动终止占用该端口的进程，或修改 webui_port 配置")
                    logger.error(f"   Windows: netstat -ano | findstr :{self.webui_port}")
                    logger.error(f"   Linux: lsof -i :{self.webui_port}")
                    return
            
            from .webui.server import WebUIServer
            self.webui_server = WebUIServer(
                self.db_manager, 
                port=self.webui_port, 
                data_dir=data_dir,
                existing_auth=auth_manager  # 复用已创建的AuthManager
            )
            self.webui_server.server_thread = threading.Thread(
                target=self.webui_server.run, 
                daemon=True, 
                name='WebUI Server'
            )
            self.webui_server.server_thread.start()
            logger.info(f"🌐 WebUI服务已启动")
            logger.info(f"   地址: http://127.0.0.1:{self.webui_port}")
            
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
    
    def _force_release_port(self, port, timeout=5):
        """强制释放指定端口
        
        尝试策略：
        1. 发送HTTP shutdown请求给旧服务
        2. 等待进程退出
        3. 验证端口是否释放
        
        Args:
            port: 端口号
            timeout: 等待超时（秒）
            
        Returns:
            bool: 是否成功释放
        """
        import urllib.request
        import time
        
        try:
            # 方法1：尝试优雅关闭旧服务
            logger.info(f"   尝试向 http://127.0.0.1:{port}/shutdown 发送关闭请求...")
            try:
                req = urllib.request.Request(
                    f'http://127.0.0.1:{port}/shutdown',
                    method='POST',
                    data=b''
                )
                urllib.request.urlopen(req, timeout=2)
                logger.info(f"   ✅ 已发送关闭请求")
            except Exception as e:
                logger.debug(f"   关闭请求失败（可能不是我们的服务）: {e}")
            
            # 等待端口释放
            logger.info(f"   等待 {timeout} 秒让端口释放...")
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                time.sleep(0.5)
                
                # 检查端口是否已释放
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                
                if result != 0:
                    logger.info(f"   ✅ 确认端口 {port} 已释放")
                    return True
            
            # 超时了还没释放
            logger.warning(f"   ⏰ 等待超时，端口仍未释放")
            return False
            
        except Exception as e:
            logger.error(f"   强制释放端口时出错: {e}")
            return False

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
        """插件销毁方法（增强版 - 确保资源完全释放）
        
        执行流程：
        1. 停止WebUI服务器（释放端口）- 即使引用丢失也要尝试
        2. 关闭数据库连接
        3. 清理缓存
        4. 确认所有资源已释放
        """
        logger.info("=" * 50)
        logger.info("记忆胶囊插件正在关闭...")
        
        # 1. 停止WebUI服务（最重要！确保端口释放）
        if self.webui_server:
            try:
                logger.info("正在停止WebUI服务器...")
                self.webui_server.stop()
                self.webui_server = None
                logger.info("✅ WebUI服务器已停止")
            except Exception as e:
                logger.error(f"停止WebUI服务器时出错: {e}")
        else:
            # 🔑 关键改进：即使webui_server是None，也尝试释放端口
            logger.info("⚠️  WebUI服务器引用为空，但仍然尝试释放端口...")
            try:
                if hasattr(self, 'webui_port') and self.webui_port:
                    self._force_release_port(self.webui_port, timeout=3)
            except Exception as e:
                logger.debug(f"强制释放端口时出错: {e}")
        
        # 2. 关闭数据库连接
        if self.db_manager:
            try:
                logger.info("正在关闭数据库连接...")
                self.db_manager.close()
                self.db_manager = None
                logger.info("✅ 数据库连接已关闭")
            except Exception as e:
                logger.error(f"关闭数据库时出错: {e}")
        
        # 3. 清理缓存
        try:
            self.relation_injection_cache.clear()
            self.last_relation_user_id = None
            logger.info("✅ 缓存已清理")
        except Exception as e:
            logger.debug(f"清理缓存时出错: {e}")
        
        logger.info("=" * 50)
        logger.info("记忆胶囊插件已完全关闭，所有资源已释放")
        logger.info("=" * 50)

    @filter.llm_tool(name="update_relationship")
    async def update_relationship(self, event, user_id, relation_type=None, summary=None, nickname=None, first_met_location=None, known_contexts=None):
        """
        【关系图谱】用于记录人与人之间的关系、印象、约定等与人相关的信息。
        
        适用场景：
        - 对某人的印象、评价、看法
        - 人的生日、习惯、喜好、忌讳
        - 人与人之间的关系（朋友、群友、管理员等）
        - 与人的约定、承诺、欠条、待还事项
        - 初次见面地点、共同群组
        - 对方说过的重要的话、承诺
        
        不适用：
        - 客观知识/技能/笔记 → 用 write_memory
        - 群规、网址、配置信息 → 用 write_memory
        - 各类客观事实 → 用 write_memory
        
        Args:
            user_id(str): 目标用户 ID
            relation_type(str): 关系定义（如：群友、伙伴、朋友等可以自己添加自身所认为的关系）
            summary(str): 对此人的印象总结、发生过的重要事情（会覆盖旧的）
            nickname(str): AI 对此人的称呼
            first_met_location(str): 初次见面地点/群ID
            known_contexts(str): 共同所在的群组/场景
            
        Returns:
            str: 更新结果
        """
        user_id = str(user_id)
        relation_type = str(relation_type) if relation_type is not None else None
        summary = str(summary) if summary is not None else None
        nickname = str(nickname) if nickname is not None else None
        first_met_location = str(first_met_location) if first_met_location is not None else None
        known_contexts = str(known_contexts) if known_contexts is not None else None
        
        try:
            # 使用增强版的关系更新（自动同步别名到映射表）
            result = await asyncio.to_thread(
                self.db_manager.update_relationship_enhanced,
                user_id, relation_type, summary, 
                nickname, first_met_location, known_contexts
            )
            logger.info(f"更新关系成功: {user_id} (已同步身份映射)")
            return result
        except Exception as e:
            logger.error(f"更新关系失败: {e}")
            return f"更新失败: {e}"

    @filter.llm_tool(name="write_memory")
    async def write_memory(self, event, content):
        """
        【记忆宫殿·小本本】用于记录客观事实、知识、技能等非人际信息。
        
        ⚠️ 重要：你只需传入 content 参数即可！不要传入 tags、category、importance 等其他参数！
           标签会自动提取，分类会自动判断，不需要你手动指定。
        
        适用场景：
        - 技术笔记、学习资料
        - 工作任务、待办事项
        - 网址、密码、配置信息
        - 群规、公告、重要通知
        - 各类客观事实和知识点
        
        不适用：
        - 人的印象/评价 → 用 update_relationship
        - 人的小事/生日/习惯 → 用 update_relationship
        - 人际间的承诺/欠条/约定 → 用 update_relationship
        
        Args:
            content(str): 要记住的客观内容（只传这个参数，不要传别的）
            
        Returns:
            str: 存储结果
        """
        if not self.config.get('memory_palace', True):
            return "记忆宫殿模块已禁用"
            
        content = str(content)
        category = None
        importance = 1
        
        category_model = self.config.get('category_model', '')
        if category_model and self.context:
            try:
                categories = await asyncio.to_thread(self.db_manager.get_memory_categories)
                if categories:
                    categories_str = '、'.join(categories)
                    prompt = f"""请分析以下内容，完成两个任务：

任务1：从分类列表中选择最合适的分类
可选分类：{categories_str}

任务2：评估内容的重要性（1-10分）
评分标准：
- 1-3分：普通记录，日常信息，临时性内容
- 4-6分：有一定参考价值，可能再次用到
- 7-8分：重要信息，关键知识点，需要牢记
- 9-10分：极其重要，核心信息，不可遗忘

内容：{content}

请严格按以下JSON格式返回，不要包含其他内容：
{{"category": "分类名称", "importance": 数字}}
"""
                    
                    provider = self.context.get_provider_by_id(category_model)
                    if provider:
                        llm_resp = await provider.text_chat(
                            prompt=prompt,
                            system_prompt="你是一个智能分析助手，擅长内容分类和重要性评估。请严格按照要求的JSON格式返回结果。"
                        )
                        
                        if llm_resp and llm_resp.completion_text:
                            response_text = llm_resp.completion_text.strip()
                            
                            try:
                                result_json = json.loads(response_text)
                                
                                predicted_category = result_json.get('category')
                                if predicted_category and predicted_category in categories:
                                    category = predicted_category
                                    logger.info(f"自动分类结果: {category}")
                                else:
                                    logger.warning(f"大模型返回的分类 '{predicted_category}' 不在分类列表中，使用默认分类")
                                
                                predicted_importance = result_json.get('importance')
                                if predicted_importance is not None:
                                    try:
                                        importance = int(predicted_importance)
                                        importance = max(1, min(10, importance))
                                        logger.info(f"自动评估重要性: {importance}")
                                    except (ValueError, TypeError):
                                        logger.warning(f"重要性值格式错误: {predicted_importance}，使用默认值1")
                                        
                            except json.JSONDecodeError:
                                logger.warning(f"无法解析LLM返回的JSON: {response_text}，使用默认值")
                        else:
                            logger.warning("大模型分析返回为空，使用默认分类和重要性")
                    else:
                        logger.warning("未找到配置的分类模型，使用默认分类")
                else:
                    logger.warning("未配置分类列表，记忆将使用默认分类和重要性")
            except Exception as e:
                logger.error(f"自动分析失败: {e}")
        else:
            logger.debug("未配置分类模型，记忆将使用默认分类和重要性")
        
        try:
            result = await asyncio.to_thread(self.db_manager.write_memory, content, category, importance=importance)
            logger.info("存储记忆成功")
            return result
        except Exception as e:
            logger.error(f"存储记忆失败: {e}")
            return f"存储失败: {e}"

    @filter.llm_tool(name="search_memory")
    async def search_memory(self, event, query, category_filter=None, limit=None):
        """
        搜索过去的记忆
        
        Args:
            query(str): 搜索关键词或句子
            category_filter(str): 分类过滤（可选）
            limit(int): 返回结果数量限制（可选，默认使用配置）
            
        Returns:
            list: 搜索结果列表
        """
        if not self.config.get('memory_palace', True):
            return "[]"
            
        query = str(query)
        category_filter = str(category_filter) if category_filter is not None else None
        if limit is not None:
            limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.search_memory, query, category_filter, limit)
            logger.info(f"搜索记忆成功，找到 {len(results)} 条结果")
            # 返回 JSON 字符串，兼容 Gemini API
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return "[]"

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
            return "[]"
            
        # 类型转换确保参数类型正确
        limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_memories, limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            # 返回 JSON 字符串，兼容 Gemini API
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return "[]"

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event):
        """
        【关系图谱·快速浏览】获取所有关系的简要列表（仅ID和昵称）。
        
        用途：
        - 快速浏览有哪些人被记录
        - 找到某人后用 search_relationship 获取详细信息
        
        注意：
        - 此方法只返回 ID 和昵称，不返回详细内容
        - 想要详细信息请用 search_relationship 搜索特定人
        
        Returns:
            list: 简要关系列表 [{"user_id": "xxx", "nickname": "昵称"}, ...]
        """
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            # 只返回 ID 和昵称，减少上下文占用
            simple_list = [{"user_id": r["user_id"], "nickname": r.get("nickname") or "未知"} for r in results]
            logger.info(f"获取所有关系成功，找到 {len(simple_list)} 条结果")
            # 返回 JSON 字符串，兼容 Gemini API
            return json.dumps(simple_list, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return "[]"

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event, user_id):
        """
        删除关系
        
        Args:
            user_id(str): 用户ID
            
        Returns:
            str: 删除结果
        """
        user_id = str(user_id)
        try:
            result = await asyncio.to_thread(self.db_manager.delete_relationship, user_id)
            logger.info(f"删除关系成功: {user_id}")
            return result
        except Exception as e:
            logger.error(f"删除关系失败: {e}")
            return f"删除失败: {e}"

    @filter.llm_tool(name="search_relationship")
    async def search_relationship(self, event, query, limit=3):
        """
        模糊搜索某人的关系信息。可以通过用户ID、昵称、关系类型等关键词搜索。
        
        Args:
            query(str): 搜索关键词（可以是ID、昵称、关系类型等）
            limit(int): 返回结果数量限制，默认3
            
        Returns:
            list: 匹配的关系列表
        """
        query = str(query)
        limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.search_relationship, query, limit)
            logger.info(f"搜索关系成功，找到 {len(results)} 条结果")
            # 返回 JSON 字符串，兼容 Gemini API
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"搜索关系失败: {e}")
            return "[]"

    @filter.llm_tool(name="backup_database")
    async def backup_database(self, event):
        """
        备份数据库
        
        Returns:
            str: 备份结果
        """
        try:
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
        
        缓存逻辑：
        - 用户切换时立即刷新并注入
        - 同一用户连续对话时按刷新时间间隔注入
        """
        try:
            # 获取用户信息
            user_id = event.get_sender_id()
            
            # 检查缓存
            import time
            current_time = time.time()
            cache_key = "relation_injection_last"
            
            should_inject = False
            
            # 逻辑：用户切换立即刷新，同用户按时间间隔
            if user_id != self.last_relation_user_id:
                # 用户切换了，立即注入
                logger.info(f"检测到用户切换: {self.last_relation_user_id} -> {user_id}，立即注入关系")
                should_inject = True
            elif cache_key in self.relation_injection_cache:
                # 同一用户，检查时间间隔
                last_injection_time = self.relation_injection_cache[cache_key]
                elapsed_time = current_time - last_injection_time
                logger.debug(f"同一用户 {user_id}，上次注入时间: {last_injection_time}, 已过时间: {elapsed_time:.1f}秒, 需等待: {self.relation_injection_refresh_time}秒")
                if elapsed_time >= self.relation_injection_refresh_time:
                    # 时间间隔到了，重新注入
                    logger.info(f"用户 {user_id} 的关系信息已超时（已过{elapsed_time:.0f}秒），重新注入")
                    should_inject = True
                else:
                    logger.info(f"用户 {user_id} 的关系信息在缓存期内（已过{elapsed_time:.0f}秒/{self.relation_injection_refresh_time}秒），跳过注入")
            else:
                # 首次对话，注入
                should_inject = True
            
            if not should_inject:
                return req
            
            # 查找用户关系信息（使用增强版查询，支持别名匹配）
            user_relation = await asyncio.to_thread(self.db_manager.get_relationship_with_identity, user_id)
            
            # 获取用户的别名列表（用于更自然的称呼）
            user_aliases = await asyncio.to_thread(self.db_manager.get_user_aliases, user_id)
            
            # 构建自然化的关系上下文
            if user_relation:
                relation_context = self._build_natural_relation_context(
                    user_relation, 
                    user_aliases,
                    user_id
                )
            else:
                # 如果查询为空，返回友好的提示格式
                relation_context = f"\n\n💬 [备注] 这位是新朋友，你可以通过 update_relationship 工具记录下关于TA的信息。\n"
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
            self.last_relation_user_id = user_id
            logger.debug(f"关系注入缓存已更新，下次对话用户: {user_id}")
            
        except Exception as e:
            logger.error(f"注入关系信息失败: {e}")
        return req
    
    def _build_natural_relation_context(self, relation, aliases=None, user_id=None):
        """构建自然化的关系上下文（让AI感觉像在和朋友聊天）
        
        根据关系深浅动态调整注入风格：
        - 新认识的朋友：简洁介绍
        - 老朋友：温馨提醒 + 重要约定/印象
        - 有待办事项：重点标注
        
        参数：
        - relation: 关系字典
        - aliases: 用户别名列表
        - user_id: 用户ID
        
        返回值：
        - 格式化后的关系上下文字符串
        """
        if not relation:
            return ""
        
        nickname = relation.get('nickname') or '朋友'
        relation_type = relation.get('relation_type') or '朋友'
        summary = relation.get('summary') or ''
        first_met = relation.get('first_met_location') or ''
        known_contexts = relation.get('known_contexts') or ''
        updated_at = relation.get('updated_at') or ''
        
        # 只使用当前昵称（简洁为主，不显示历史别名）
        # AI内部可以通过 get_user_aliases 查看完整别名列表
        display_name = nickname  # 只显示1个当前名字
        
        # 计算关系深浅（基于更新时间和信息完整度）
        from datetime import datetime
        days_since_update = 0
        if updated_at:
            try:
                update_time = datetime.strptime(str(updated_at)[:19], '%Y-%m-%d %H:%M:%S')
                days_since_update = (datetime.now() - update_time).days
            except:
                pass
        
        info_completeness = sum([
            bool(summary),
            bool(first_met),
            bool(known_contexts),
            bool(relation_type and relation_type != '未知')
        ])
        
        # 根据关系深浅选择注入风格
        if info_completeness <= 1 and (not updated_at or days_since_update > 30):
            # 风格A：新朋友或很久没联系 —— 简洁介绍
            context = f"""
📝 [关于对话对象]
你正在和【{display_name}】聊天，TA是你的{relation_type}。
你们认识不久，可以多了解TA并记录下来。
"""
        
        elif info_completeness >= 3 and days_since_update < 7:
            # 风格B：老朋友且最近活跃 —— 温馨提醒 + 详细信息
            context_parts = [
                f"\n👤 [当前对话者] {display_name}",
                f"",
                f"🏷️ 关系定位: {relation_type}"
            ]
            
            if summary:
                context_parts.append(f"💭 印象笔记: {summary}")
            
            if first_met:
                context_parts.append(f"📍 相识于: {first_met}")
            
            if known_contexts:
                contexts_list = known_contexts.split(',')[:3]
                contexts_str = '、'.join(contexts_list)
                context_parts.append(f"👥 共同场景: {contexts_str}")
            
            # 检查是否有重要关键词（约定、承诺等）
            important_keywords = ['约定', '承诺', '重要', '记得', '提醒', '待办']
            has_important = any(kw in str(summary) for kw in important_keywords)
            
            if has_important:
                context_parts.append("")
                context_parts.append("⚠️ 注意: 你们之间有重要的约定或事项，请留意！")
            
            context = '\n'.join(context_parts) + '\n'
        
        else:
            # 风格C：普通关系 —— 平衡的信息量
            context = f"""
📋 [人物档案]
• 称呼: {display_name}
• 关系: {relation_type}
"""
            if summary:
                context += f"• 特点: {summary}\n"
            if known_contexts:
                context += f"• 出没地: {known_contexts.split(',')[0] if ',' in known_contexts else known_contexts}\n"
        
        return context

# 外部接口，供其他插件调用
# 注意：这些函数已在 __init__.py 中重新定义，使用单例模式
