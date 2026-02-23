from flask import Flask, render_template, jsonify, request
import threading
import time
from astrbot.api import logger

class WebUIServer:
    def __init__(self, db_manager, port=5000):
        self.app = Flask(__name__)
        self.db_manager = db_manager
        self.port = port
        self.running = False
        self.setup_routes()

    def setup_routes(self):
        """设置路由"""
        @self.app.route('/')
        def index():
            from flask import make_response
            response = make_response(render_template('index.html'))
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
            notes = self.db_manager.get_all_plugin_data()
            return jsonify(notes)

        @self.app.route('/api/relations')
        def api_relations():
            """获取关系列表"""
            relations = self.db_manager.get_all_relations()
            return jsonify(relations)

        @self.app.route('/api/notes', methods=['POST'])
        def api_add_note():
            """添加笔记"""
            data = request.json
            result = self.db_manager.store_plugin_data(
                content=data.get('content', ''),
                metadata=data.get('metadata', {})
            )
            return jsonify({'result': result})

        @self.app.route('/api/notes/<int:note_id>', methods=['DELETE'])
        def api_delete_note(note_id):
            """删除笔记"""
            result = self.db_manager.delete_plugin_data(note_id)
            return jsonify({'result': result})

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

    def stop(self):
        """停止服务器"""
        self.running = False
        # Flask的开发服务器不支持优雅停止，这里只是标记状态
        print("WebUI服务器已停止")
