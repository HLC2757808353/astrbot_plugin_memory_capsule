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
            return render_template('index.html')

        @self.app.route('/notes')
        def notes():
            return render_template('notes.html')

        @self.app.route('/relations')
        def relations():
            return render_template('relations.html')

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
                plugin_name=data.get('plugin_name', 'webui'),
                data_type=data.get('data_type', 'note'),
                content=data.get('content', ''),
                metadata=data.get('metadata', {})
            )
            return jsonify({'result': result})

        @self.app.route('/api/notes/<int:note_id>', methods=['DELETE'])
        def api_delete_note(note_id):
            """删除笔记"""
            result = self.db_manager.delete_plugin_data(note_id)
            return jsonify({'result': result})

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
