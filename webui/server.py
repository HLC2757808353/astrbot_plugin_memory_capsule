from flask import Flask, render_template, jsonify, request, make_response
import threading
import time
import yaml
import os
import socket

# 容错处理
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
        # ... (这里保留你原来的路由代码不变，为了简洁省略重复部分) ...
        @self.app.route('/')
        def index():
            response = make_response(render_template('index.html', version=self.version))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/notes')
        def notes():
            response = make_response(render_template('notes.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/relations')
        def relations():
            response = make_response(render_template('relations.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/api/notes')
        def api_notes():
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            category = request.args.get('category')
            notes = self.db_manager.get_all_plugin_data(limit, offset, category)
            total_notes = self.db_manager.get_plugin_data_count(category)
            return jsonify({
                'notes': notes, 'total': total_notes, 'page': page,
                'limit': limit, 'total_pages': (total_notes + limit - 1) // limit
            })

        @self.app.route('/api/relations')
        def api_relations():
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            relations = self.db_manager.get_all_relations(limit, offset)
            total_relations = self.db_manager.get_relations_count()
            return jsonify({
                'relations': relations, 'total': total_relations, 'page': page,
                'limit': limit, 'total_pages': (total_relations + limit - 1) // limit
            })

        @self.app.route('/api/notes', methods=['POST'])
        def api_add_note():
            data = request.json
            result = self.db_manager.store_plugin_data(
                content=data.get('content', ''),
                metadata=data.get('metadata', {}),
                category=data.get('category', '')
            )
            return jsonify({'result': result})

        @self.app.route('/api/notes/<int:note_id>', methods=['DELETE'])
        def api_delete_note(note_id):
            result = self.db_manager.delete_plugin_data(note_id)
            return jsonify({'result': result})

        @self.app.route('/api/notes/search')
        def api_search_notes():
            query = request.args.get('q')
            category = request.args.get('category')
            notes = self.db_manager.query_plugin_data(query, category=category)
            return jsonify(notes)

        @self.app.route('/api/relations', methods=['POST'])
        def api_add_relation():
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
            result = self.db_manager.delete_relation(user_id, platform)
            return jsonify({'result': result})

        @self.app.route('/api/relations/search')
        def api_search_relations():
            query = request.args.get('q', '')
            search_type = request.args.get('type', 'name')
            conn = self.db_manager._get_connection()
            cursor = conn.cursor()
            if search_type == 'id':
                cursor.execute('SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at FROM relations WHERE user_id = ?', (query,))
            elif search_type in ['group_id', 'group_name']:
                cursor.execute('SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at FROM relations WHERE group_id LIKE ?', (f"%{query}%",))
            else:
                cursor.execute('SELECT user_id, nickname, group_id, platform, impression_summary, remark, favor_level, created_at FROM relations WHERE nickname LIKE ? OR remark LIKE ?', (f"%{query}%", f"%{query}%"))
            results = cursor.fetchall()
            conn.close()
            relation_list = [{"user_id": r[0], "nickname": r[1], "group_id": r[2], "platform": r[3], "impression": r[4], "remark": r[5], "favor_level": r[6], "created_at": r[7]} for r in results]
            return jsonify(relation_list)

        @self.app.route('/shutdown', methods=['POST'])
        def shutdown():
            """关闭服务器的内部接口"""
            func = request.environ.get('werkzeug.server.shutdown')
            if func is None:
                # 尝试兼容旧版本或其他WSGI服务器
                try:
                    from werkzeug.server import shutdown_server
                    shutdown_server()
                except:
                    logger.error("无法关闭服务器：缺少 shutdown 函数")
            else:
                func()
            return 'Server shutting down...'

    def run(self):
        """运行服务器"""
        self.running = True
        try:
            # 注意：这里移除了错误的端口检查逻辑
            # 设置 use_reloader=False 避免多进程问题，设置 threaded=True 支持并发
            # Werkzeug 默认开启 SO_REUSEADDR，这有助于快速重启
            self.app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False, threaded=True)
        except OSError as e:
            if "Address already in use" in str(e):
                logger.error(f"端口 {self.port} 已被占用，请检查是否有残留进程或修改端口配置。")
            else:
                logger.error(f"WebUI服务器运行失败: {e}")
        except Exception as e:
            logger.error(f"WebUI服务器运行失败: {e}")
        finally:
            self.running = False

    def stop(self):
        """停止服务器并释放端口（确保线程结束）"""
        if not self.running and not (self.server_thread and self.server_thread.is_alive()):
            logger.info("WebUI服务器未运行，无需停止。")
            return

        logger.info(f"正在停止 WebUI 服务器 (端口 {self.port})...")
        
        # 1. 尝试通过 HTTP 请求触发内部关闭
        try:
            import requests
            # 设置超时时间短一点，防止卡死
            requests.post(f"http://localhost:{self.port}/shutdown", timeout=2)
        except Exception:
            # 忽略错误（服务器可能已经停了，或者网络不通）
            pass

        # 2. 【关键修改】等待线程真正结束
        if self.server_thread and self.server_thread.is_alive():
            # 等待线程结束，最多等5秒
            self.server_thread.join(timeout=5)
            if self.server_thread.is_alive():
                logger.warning(f"WebUI 服务器线程在 5 秒后未能停止，可能存在僵尸线程。")
            else:
                logger.info(f"WebUI 服务器线程已完全停止，端口 {self.port} 已释放。")
        
        self.running = False
