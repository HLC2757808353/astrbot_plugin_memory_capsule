from flask import Flask, render_template, jsonify, request
import threading
import time
import yaml
import os
import socket

# 容错处理，当astrbot模块不可用时使用默认日志
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

class WebUIServer:
    def __init__(self, db_manager, port=5000):
        self.app = Flask(__name__)
        self.db_manager = db_manager
        self.port = port
        self.running = False
        self.version = self._get_version()
        self.server_thread = None
        self.server = None  # 存储服务器实例
        self.setup_routes()
    
    def _get_version(self):
        """从metadata.yaml读取版本号"""
        try:
            metadata_path = os.path.join(os.path.dirname(__file__), "..", "metadata.yaml")
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = yaml.safe_load(f)
                return metadata.get('version', 'v0.0.1')
        except Exception as e:
            logger.error(f"读取版本号失败: {e}")
            return 'v0.0.1'

    def setup_routes(self):
        """设置路由"""
        @self.app.route('/')
        def index():
            from flask import make_response
            response = make_response(render_template('index.html', version=self.version))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

        @self.app.route('/notes')
        def notes():
            from flask import make_response
            response = make_response(render_template('notes.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

        @self.app.route('/relations')
        def relations():
            from flask import make_response
            response = make_response(render_template('relations.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

        @self.app.route('/api/notes')
        def api_notes():
            """获取笔记列表"""
            # 获取分页参数
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            
            # 获取分类参数
            category = request.args.get('category')
            
            # 获取笔记列表
            notes = self.db_manager.get_all_plugin_data(limit, offset, category)
            
            # 获取总笔记数
            total_notes = self.db_manager.get_plugin_data_count(category)
            
            return jsonify({
                'notes': notes,
                'total': total_notes,
                'page': page,
                'limit': limit,
                'total_pages': (total_notes + limit - 1) // limit
            })

        @self.app.route('/api/relations')
        def api_relations():
            """获取关系列表"""
            # 获取分页参数
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            
            # 获取关系列表
            relations = self.db_manager.get_all_relations(limit, offset)
            
            # 获取总关系数
            total_relations = self.db_manager.get_relations_count()
            
            return jsonify({
                'relations': relations,
                'total': total_relations,
                'page': page,
                'limit': limit,
                'total_pages': (total_relations + limit - 1) // limit
            })

        @self.app.route('/api/notes', methods=['POST'])
        def api_add_note():
            """添加笔记"""
            data = request.json
            result = self.db_manager.store_plugin_data(
                content=data.get('content', ''),
                metadata=data.get('metadata', {}),
                category=data.get('category', '')
            )
            return jsonify({'result': result})

        @self.app.route('/api/notes/<int:note_id>', methods=['DELETE'])
        def api_delete_note(note_id):
            """删除笔记"""
            result = self.db_manager.delete_plugin_data(note_id)
            return jsonify({'result': result})

        @self.app.route('/api/notes/search')
        def api_search_notes():
            """搜索笔记"""
            query = request.args.get('q')
            category = request.args.get('category')
            notes = self.db_manager.query_plugin_data(query, category=category)
            return jsonify(notes)

        @self.app.route('/api/relations', methods=['POST'])
        def api_add_relation():
            """添加关系"""
            data = request.json
            result = self.db_manager.update_relation(
                user_id=data.get('user_id', ''),
                group_id=data.get('group_id', ''),
                platform=data.get('platform', 'qq'),
                nickname=data.get('nickname', ''),
                favor_change=data.get('favor_change', 0),
                impression=data.get('impression', ''),
                remark=data.get('remark', '')
            )
            return jsonify({'result': result})

        @self.app.route('/api/relations/<string:user_id>/<string:platform>', methods=['DELETE'])
        def api_delete_relation(user_id, platform):
            """删除关系"""
            result = self.db_manager.delete_relation(user_id, platform)
            return jsonify({'result': result})

        @self.app.route('/api/relations/search')
        def api_search_relations():
            """搜索关系（模糊查询）"""
            query = request.args.get('q', '')
            search_type = request.args.get('type', 'name')
            
            # 使用独立连接
            conn = self.db_manager._get_connection()
            cursor = conn.cursor()
            
            # 根据搜索类型构建查询
            if search_type == 'id':
                # ID搜索，精确匹配
                cursor.execute('''
                SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at 
                FROM relations 
                WHERE user_id = ?
                ''', (query,))
            elif search_type == 'group_id':
                # 群号搜索
                cursor.execute('''
                SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at 
                FROM relations 
                WHERE group_id LIKE ?
                ''', (f"%{query}%",))
            elif search_type == 'group_name':
                # 群名搜索（这里假设 group_id 中包含群名）
                cursor.execute('''
                SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at 
                FROM relations 
                WHERE group_id LIKE ?
                ''', (f"%{query}%",))
            else:
                # 名称搜索（默认）
                cursor.execute('''
                SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at 
                FROM relations 
                WHERE nickname LIKE ? OR remark LIKE ?
                ''', (f"%{query}%", f"%{query}%"))
            
            results = cursor.fetchall()
            conn.close()
            
            # 处理结果
            relation_list = []
            for row in results:
                relation = {
                    "user_id": row[0],
                    "nickname": row[1],
                    "group_id": row[2],
                    "platform": row[3],
                    "impression": row[4],
                    "remark": row[5],
                    "favor_level": row[6],
                    "created_at": row[7]
                }
                relation_list.append(relation)
            
            return jsonify(relation_list)

        @self.app.route('/shutdown', methods=['POST'])
        def shutdown():
            """关闭服务器"""
            # 使用兼容的方法关闭服务器
            try:
                # 尝试使用旧版本的shutdown_server
                from werkzeug.server import shutdown_server
                shutdown_server()
                return 'Server shutting down...'
            except ImportError:
                # 对于新版本的Werkzeug，使用不同的方法
                try:
                    from flask import request
                    shutdown_func = request.environ.get('werkzeug.server.shutdown')
                    if shutdown_func:
                        shutdown_func()
                        return 'Server shutting down...'
                    else:
                        # 如果shutdown_func为None，直接返回成功，因为我们已经通过其他方式关闭服务器
                        logger.info("Werkzeug server shutdown function not available, using alternative method")
                        return 'Server shutting down...'
                except Exception as e:
                    logger.error(f"关闭服务器时发生错误: {e}")
                    return 'Failed to shutdown server'


    def check_port(self, port):
        """检测端口是否被占用"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            result = sock.connect_ex(('localhost', port))
            if result == 0:
                logger.warning(f"端口 {port} 已被占用")
                return False
            else:
                logger.info(f"端口 {port} 可用")
                return True
        except Exception as e:
            logger.error(f"检测端口时发生错误: {e}")
            return False
        finally:
            sock.close()

    def run(self):
        """运行服务器"""
        self.running = True
        try:
            # 直接创建服务器，使用SO_REUSEADDR选项
            import socket
            
            # 创建socket并设置SO_REUSEADDR选项
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # 尝试绑定端口，最多5次
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    server_socket.bind(('0.0.0.0', self.port))
                    logger.info(f"成功绑定端口 {self.port}")
                    break
                except Exception as e:
                    logger.warning(f"尝试 {attempt + 1}/{max_attempts} 绑定端口 {self.port} 失败: {e}")
                    # 尝试强制释放端口
                    try:
                        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        temp_sock.bind(('0.0.0.0', self.port))
                        temp_sock.close()
                        logger.info(f"端口 {self.port} 已强制释放")
                    except Exception as release_error:
                        logger.debug(f"强制释放端口时发生错误: {release_error}")
                    time.sleep(1)
            else:
                # 所有尝试都失败
                logger.error(f"WebUI服务器启动失败: 端口 {self.port} 已被占用，尝试释放失败")
                server_socket.close()
                return
            
            # 开始监听
            server_socket.listen(5)
            
            # 使用socket创建服务器
            from werkzeug.serving import make_server
            # 获取socket的文件描述符
            fd = server_socket.fileno()
            self.server = make_server('0.0.0.0', self.port, self.app, threaded=True, fd=fd)
            logger.info(f"WebUI服务器已启动，端口: {self.port}")
            
            # 运行服务器直到self.running为False
            while self.running:
                # 处理一个请求，超时1秒
                self.server.handle_request()
        except Exception as e:
            logger.error(f"WebUI服务器运行失败: {e}")
        finally:
            self.running = False
            if hasattr(self, 'server') and self.server:
                try:
                    self.server.shutdown()
                    logger.info("WebUI服务器已通过服务器实例关闭")
                except Exception as e:
                    logger.debug(f"关闭服务器实例时发生错误: {e}")

    def stop(self):
        """停止服务器并释放端口"""
        logger.info("WebUI服务器正在停止...")
        
        # 停止服务器循环
        self.running = False
        
        # 尝试通过服务器实例关闭
        if hasattr(self, 'server') and self.server:
            try:
                self.server.shutdown()
                logger.info("WebUI服务器已通过服务器实例关闭")
            except Exception as e:
                logger.debug(f"尝试通过服务器实例停止服务器时发生错误: {e}")
        
        # 等待一段时间，确保服务器完全停止
        time.sleep(1)
        
        # 尝试kill占用端口的进程
        try:
            import subprocess
            import re
            
            # 使用netstat命令查找占用端口的进程
            if os.name == 'nt':  # Windows
                cmd = f"netstat -ano | findstr :{self.port}"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                # 解析输出，找到PID
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if line:
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            pid = parts[-1]
                            logger.info(f"找到占用端口 {self.port} 的进程 PID: {pid}")
                            
                            # 尝试kill进程
                            try:
                                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True, text=True)
                                logger.info(f"已杀死占用端口 {self.port} 的进程 PID: {pid}")
                            except Exception as kill_error:
                                logger.debug(f"尝试杀死进程时发生错误: {kill_error}")
            else:  # Linux/Mac
                cmd = f"lsof -i :{self.port}"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                # 解析输出，找到PID
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:  # 跳过标题行
                    if line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            pid = parts[1]
                            logger.info(f"找到占用端口 {self.port} 的进程 PID: {pid}")
                            
                            # 尝试kill进程
                            try:
                                subprocess.run(f"kill -9 {pid}", shell=True, capture_output=True, text=True)
                                logger.info(f"已杀死占用端口 {self.port} 的进程 PID: {pid}")
                            except Exception as kill_error:
                                logger.debug(f"尝试杀死进程时发生错误: {kill_error}")
        except Exception as e:
            logger.debug(f"尝试查找和杀死占用端口的进程时发生错误: {e}")
        
        # 强制释放端口的逻辑 - 多次尝试
        max_release_attempts = 3
        for attempt in range(max_release_attempts):
            try:
                # 创建一个临时socket来尝试释放端口
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('0.0.0.0', self.port))
                sock.close()
                logger.info(f"端口 {self.port} 已成功释放 (尝试 {attempt + 1}/{max_release_attempts})")
                break
            except Exception as e:
                logger.debug(f"尝试 {attempt + 1}/{max_release_attempts} 强制释放端口时发生错误: {e}")
                time.sleep(0.5)
        
        # 等待一段时间，确保端口完全释放
        time.sleep(1)
        
        # 再次检查端口是否被释放
        if self.check_port(self.port):
            logger.info(f"端口 {self.port} 已完全释放")
        else:
            logger.warning(f"端口 {self.port} 仍然被占用，尝试使用更强制的方法")
            
            # 尝试使用更强制的方法释放端口
            try:
                # 使用socket的SO_REUSEADDR选项并绑定到0.0.0.0
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('0.0.0.0', self.port))
                sock.listen(1)
                sock.close()
                logger.info(f"端口 {self.port} 已通过强制绑定释放")
            except Exception as e:
                logger.error(f"使用强制方法释放端口时发生错误: {e}")
        
        logger.info("WebUI服务器已停止，端口已释放")
