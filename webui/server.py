from flask import Flask, render_template, jsonify, request
import threading
import time
import yaml
import os
import socket
from astrbot.api import logger

class WebUIServer:
    def __init__(self, db_manager, port=5000):
        self.app = Flask(__name__)
        self.db_manager = db_manager
        self.port = port
        self.running = False
        self.version = self._get_version()
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

        @self.app.route('/api/relations/<string:user_id>/<string:group_id>/<string:platform>', methods=['DELETE'])
        def api_delete_relation(user_id, group_id, platform):
            """删除关系"""
            result = self.db_manager.delete_relation(user_id, group_id, platform)
            return jsonify({'result': result})

        @self.app.route('/api/relations/search')
        def api_search_relations():
            """搜索关系（模糊查询）"""
            query = request.args.get('q', '')
            relations = self.db_manager.query_relation(query)
            return jsonify(relations)

    def run(self):
        """运行服务器"""
        self.running = True
        try:
            # 使用线程运行，避免阻塞主线程
            self.app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"WebUI服务器运行失败: {e}")
        finally:
            self.running = False

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
            # 尝试检测并释放端口，最多5次
            max_attempts = 5
            for attempt in range(max_attempts):
                if self.check_port(self.port):
                    break
                logger.info(f"尝试释放端口 {self.port}，第 {attempt + 1} 次")
                time.sleep(5)
            else:
                # 所有尝试都失败
                logger.error(f"WebUI服务器启动失败: 端口 {self.port} 已被占用，尝试释放失败")
                return
            
            # 使用线程运行，避免阻塞主线程
            self.app.run(host='0.0.0.0', port=self.port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error(f"WebUI服务器运行失败: {e}")
        finally:
            self.running = False

    def stop(self):
        """停止服务器并释放端口"""
        self.running = False
        logger.info("WebUI服务器已停止，端口已释放")
        # 尝试强制关闭服务器线程（如果可能）
        try:
            # 对于Flask开发服务器，我们需要通过其他方式停止
            # 这里我们可以尝试获取服务器线程并终止它
            import threading
            for thread in threading.enumerate():
                if thread.name == 'WebUI Server':
                    thread.join(timeout=1)
                    break
        except Exception as e:
            # 忽略错误，因为在某些环境中可能不支持
            logger.debug(f"尝试停止服务器线程时发生错误: {e}")
