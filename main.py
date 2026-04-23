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
        
        logger.info(f"正在启动WebUI服务，配置端口: {self.webui_port}")
        
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        
        try:
            from .webui.auth import AuthManager
            auth_manager = AuthManager(data_dir)
            logger.info(f"认证信息已生成")
        except Exception as e:
            logger.error(f"生成认证信息失败: {e}")
            auth_manager = None
        
        try:
            import socket
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', self.webui_port))
            sock.close()
            
            if result == 0:
                logger.warning(f"端口 {self.webui_port} 已被占用，尝试释放...")
                
                if self._force_release_port(self.webui_port):
                    logger.info(f"端口 {self.webui_port} 已释放")
                else:
                    logger.error(f"端口 {self.webui_port} 无法释放，请手动处理或修改端口配置")
                    return
            
            from .webui.server import WebUIServer
            self.webui_server = WebUIServer(
                self.db_manager, 
                port=self.webui_port, 
                data_dir=data_dir,
                existing_auth=auth_manager
            )
            self.webui_server.server_thread = threading.Thread(
                target=self.webui_server.run, 
                daemon=True, 
                name='WebUI Server'
            )
            self.webui_server.server_thread.start()
            logger.info(f"WebUI服务已启动: http://127.0.0.1:{self.webui_port}")
            
        except Exception as e:
            logger.error(f"启动WebUI服务失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
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
        import socket
        
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
        记录人际关系信息（印象、约定、习惯等）。
        
        适用：人的印象/评价、生日习惯、约定承诺
        不适用：客观知识/技能/笔记 → 用 write_memory
        
        Args:
            user_id(str): 用户ID
            relation_type(str): 关系类型（如：群友、朋友）
            summary(str): 印象总结
            nickname(str): 称呼
            first_met_location(str): 初次见面地点
            known_contexts(str): 共同群组（新群会追加，多个用逗号分隔）
            
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
        记录客观信息（知识、笔记、网址、配置等）。
        
        只需传入content参数，标签和分类会自动处理。
        
        适用：技术笔记、学习资料、工作任务、网址配置、群规公告
        不适用：人的印象/约定 → 用 update_relationship
        
        Args:
            content(str): 要记住的内容
            
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
        搜索记忆内容。
        
        Args:
            query(str): 搜索关键词
            category_filter(str): 分类过滤（可选）
            limit(int): 结果数量限制（可选）
            
        Returns:
            dict: {"results": [...]}
        """
        if not self.config.get('memory_palace', True):
            return '{"results": []}'
            
        query = str(query)
        category_filter = str(category_filter) if category_filter is not None else None
        if limit is not None:
            limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.search_memory, query, category_filter, limit)
            logger.info(f"搜索记忆成功，找到 {len(results)} 条结果")
            return json.dumps({"results": results}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"搜索记忆失败: {e}")
            return '{"results": []}'

    @filter.llm_tool(name="delete_memory")
    async def delete_memory(self, event, memory_id):
        """
        删除指定记忆。
        
        Args:
            memory_id(int): 记忆ID
            
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
        获取所有记忆列表。
        
        Args:
            limit(int): 数量限制，默认20
            
        Returns:
            dict: {"memories": [...]}
        """
        if not self.config.get('memory_palace', True):
            return '{"memories": []}'
            
        limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_memories, limit)
            logger.info(f"获取所有记忆成功，找到 {len(results)} 条结果")
            return json.dumps({"memories": results}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"获取所有记忆失败: {e}")
            return '{"memories": []}'

    @filter.llm_tool(name="get_all_relationships")
    async def get_all_relationships(self, event):
        """
        获取所有关系列表（仅ID和昵称）。
        想要详细信息请用 search_relationship。
        
        Returns:
            dict: {"relationships": [{"user_id": "xxx", "nickname": "昵称"}, ...]}
        """
        try:
            results = await asyncio.to_thread(self.db_manager.get_all_relationships)
            simple_list = [{"user_id": r["user_id"], "nickname": r.get("nickname") or "未知"} for r in results]
            logger.info(f"获取所有关系成功，找到 {len(simple_list)} 条结果")
            return json.dumps({"relationships": simple_list}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"获取所有关系失败: {e}")
            return '{"relationships": []}'

    @filter.llm_tool(name="delete_relationship")
    async def delete_relationship(self, event, user_id):
        """
        删除指定用户的关系记录。
        
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
        搜索某人的关系信息（支持ID、昵称、关系类型）。
        
        Args:
            query(str): 搜索关键词
            limit(int): 结果数量，默认3
            
        Returns:
            dict: {"results": [...]}
        """
        query = str(query)
        limit = int(limit)
        try:
            results = await asyncio.to_thread(self.db_manager.search_relationship, query, limit)
            logger.info(f"搜索关系成功，找到 {len(results)} 条结果")
            return json.dumps({"results": results}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"搜索关系失败: {e}")
            return '{"results": []}'

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
        - 刷新时间设为 -1 时每次都注入
        """
        try:
            user_id = event.get_sender_id()
            
            import time
            current_time = time.time()
            cache_key = "relation_injection_last"
            
            should_inject = False
            
            if self.relation_injection_refresh_time == -1:
                should_inject = True
                logger.info(f"关系注入刷新时间设为-1，每次都注入 (用户: {user_id})")
            elif user_id != self.last_relation_user_id:
                logger.info(f"检测到用户切换: {self.last_relation_user_id} -> {user_id}，立即注入关系")
                should_inject = True
            elif cache_key in self.relation_injection_cache:
                last_injection_time = self.relation_injection_cache[cache_key]
                elapsed_time = current_time - last_injection_time
                logger.debug(f"同一用户 {user_id}，已过{elapsed_time:.0f}秒/{self.relation_injection_refresh_time}秒")
                if elapsed_time >= self.relation_injection_refresh_time:
                    logger.info(f"用户 {user_id} 关系信息已超时，重新注入")
                    should_inject = True
                else:
                    logger.info(f"用户 {user_id} 关系信息在缓存期内，跳过注入")
            else:
                should_inject = True
            
            if not should_inject:
                return req
            
            user_relation = await asyncio.to_thread(self.db_manager.get_relationship_with_identity, user_id)
            user_aliases = await asyncio.to_thread(self.db_manager.get_user_aliases, user_id)
            
            current_group = ""
            try:
                current_group = event.get_group_id() or ""
            except:
                pass
            
            if user_relation:
                relation_context = self._build_natural_relation_context(
                    user_relation, 
                    user_aliases,
                    user_id,
                    current_group
                )
            else:
                relation_context = f"\n【当前对话对象】\n用户ID: {user_id}\n状态: 初次见面\n提示: 如果对话有意义，可用 update_relationship 记录TA\n"
                logger.info(f"用户 {user_id} 暂无关系信息")
            
            injection_method = self.config.get('context_inject_position', 'user_prompt')
            
            if injection_method == 'system_prompt':
                req.system_prompt = (req.system_prompt or "") + relation_context
                logger.info(f"成功注入用户 {user_id} 的关系信息到系统提示词")
            elif injection_method == 'user_prompt':
                req.prompt = relation_context + '\n' + (req.prompt or "")
                logger.info(f"成功注入用户 {user_id} 的关系信息到用户提示词")
            elif injection_method == 'insert_system_prompt':
                if hasattr(req, 'messages'):
                    req.messages.insert(0, {
                        'role': 'system',
                        'content': relation_context
                    })
                    logger.info(f"成功向上下文列表添加用户 {user_id} 的关系信息系统消息")
                else:
                    req.system_prompt = (req.system_prompt or "") + relation_context
                    logger.info(f"成功注入用户 {user_id} 的关系信息到系统提示词")
            else:
                req.prompt = relation_context + '\n' + (req.prompt or "")
                logger.info(f"成功注入用户 {user_id} 的关系信息到用户提示词")
            
            self.relation_injection_cache[cache_key] = current_time
            self.last_relation_user_id = user_id
            
        except Exception as e:
            logger.error(f"注入关系信息失败: {e}")
        return req
    
    def _build_natural_relation_context(self, relation, aliases=None, user_id=None, current_group=""):
        """构建简洁清晰的关系上下文"""
        if not relation:
            return ""
        
        nickname = relation.get('nickname') or '朋友'
        relation_type = relation.get('relation_type') or '朋友'
        summary = relation.get('summary') or ''
        first_met = relation.get('first_met_location') or ''
        known_contexts = relation.get('known_contexts') or ''
        
        lines = [
            "\n【当前对话对象】",
            f"昵称: {nickname}",
            f"关系: {relation_type}"
        ]
        
        if summary:
            lines.append(f"印象: {summary}")
        if first_met:
            lines.append(f"相识: {first_met}")
        
        if known_contexts:
            contexts_list = [c.strip() for c in known_contexts.split(',') if c.strip()]
            if len(contexts_list) == 1:
                lines.append(f"场景: {contexts_list[0]}")
            elif len(contexts_list) > 1:
                if current_group and current_group in known_contexts:
                    other_contexts = [c for c in contexts_list if c != current_group]
                    if other_contexts:
                        lines.append(f"共同群: {current_group} (另在{len(other_contexts)}个群见过)")
                    else:
                        lines.append(f"共同群: {current_group}")
                else:
                    lines.append(f"共同群: {'、'.join(contexts_list[:3])}")
                    if len(contexts_list) > 3:
                        lines[-1] += f" 等{len(contexts_list)}个"
        
        important_keywords = ['约定', '承诺', '重要', '记得', '提醒', '待办']
        if any(kw in str(summary) for kw in important_keywords):
            lines.append("注意: 有重要约定事项")
        
        return '\n'.join(lines) + '\n'

# 外部接口，供其他插件调用
# 注意：这些函数已在 __init__.py 中重新定义，使用单例模式
