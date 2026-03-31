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
        self._own_pid = os.getpid()
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



        @self.app.route('/memories')
        def memories():
            response = make_response(render_template('memories.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/relationships')
        def relationships():
            response = make_response(render_template('relationships.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/settings')
        def settings():
            response = make_response(render_template('settings.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/api/memories')
        def api_memories():
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            category = request.args.get('category')
            memories = self.db_manager.get_all_memories(limit, offset, category)
            total_memories = self.db_manager.get_memories_count(category)
            return jsonify({
                'memories': memories, 'total': total_memories, 'page': page,
                'limit': limit, 'total_pages': (total_memories + limit - 1) // limit
            })

        @self.app.route('/api/relationships')
        def api_relationships():
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            relationships = self.db_manager.get_all_relationships(limit, offset)
            total_relationships = self.db_manager.get_relationships_count()
            return jsonify({
                'relationships': relationships, 'total': total_relationships, 'page': page,
                'limit': limit, 'total_pages': (total_relationships + limit - 1) // limit
            })

        @self.app.route('/api/memories', methods=['POST'])
        def api_add_memory():
            data = request.json
            result = self.db_manager.write_memory(
                content=data.get('content', ''),
                category=data.get('category'),
                tags=data.get('tags', ''),
                importance=data.get('importance', 5)
            )
            return jsonify({'result': result})

        @self.app.route('/api/memories/<memory_id>', methods=['DELETE'])
        def api_delete_memory(memory_id):
            result = self.db_manager.delete_memory(memory_id)
            return jsonify({'result': result})
        
        @self.app.route('/api/memories/<memory_id>', methods=['PUT'])
        def api_update_memory(memory_id):
            data = request.json
            result = self.db_manager.update_memory(
                memory_id=memory_id,
                content=data.get('content', ''),
                category=data.get('category'),
                tags=data.get('tags', ''),
                importance=data.get('importance', 5)
            )
            return jsonify({'result': result})

        @self.app.route('/api/memories/search')
        def api_search_memories():
            """搜索记忆"""
            query = request.args.get('q')
            memories = self.db_manager.search_memory(query)
            return jsonify(memories)
        
        @self.app.route('/api/tags')
        def api_tags():
            """获取所有标签"""
            tags = self.db_manager.get_all_tags()
            return jsonify(tags)
        
        @self.app.route('/api/categories')
        def api_categories():
            """获取所有分类"""
            # 从配置中获取分类
            categories = self.db_manager.get_memory_categories()
            return jsonify(categories)

        @self.app.route('/api/relationships', methods=['POST'])
        def api_add_relationship():
            data = request.json
            result = self.db_manager.update_relationship(
                user_id=data.get('user_id', ''),
                relation_type=data.get('relation_type', ''),
                summary_update=data.get('summary_update', ''),
                nickname=data.get('nickname', ''),
                first_met_location=data.get('first_met_location'),
                known_contexts=data.get('known_contexts')
            )
            return jsonify({'result': result})

        @self.app.route('/api/relationships/<string:user_id>', methods=['DELETE'])
        def api_delete_relationship(user_id):
            result = self.db_manager.delete_relationship(user_id)
            return jsonify({'result': result})

        @self.app.route('/api/relationships/search')
        def api_search_relationships():
            query = request.args.get('q', '')
            conn = self.db_manager._get_connection()
            cursor = conn.cursor()
            # 首先尝试精确匹配用户ID
            cursor.execute('SELECT * FROM relationships WHERE user_id = ?', (query,))
            results = cursor.fetchall()
            
            # 如果没有精确匹配，再进行模糊搜索
            if not results:
                cursor.execute('SELECT * FROM relationships WHERE nickname LIKE ? OR relation_type LIKE ? OR summary LIKE ? OR first_met_location LIKE ? OR known_contexts LIKE ?', 
                              (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%"))
                results = cursor.fetchall()
            
            conn.close()
            relationship_list = []
            for r in results:
                relationship = {
                    "user_id": r[0],
                    "nickname": r[1],
                    "relation_type": r[2],
                    "summary": r[3],
                    "first_met_location": r[4],
                    "known_contexts": r[5],
                    "updated_at": r[6]
                }
                relationship_list.append(relationship)
            return jsonify(relationship_list)

        @self.app.route('/api/settings', methods=['GET'])
        def api_get_settings(self):
            """获取系统设置"""
            try:
                # 读取配置文件
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                logger.info(f"尝试读取配置文件: {config_path}")
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        import json
                        config = json.load(f)
                        logger.info(f"配置文件读取成功，配置项: {list(config.keys())}")
                        # 提取默认值，返回扁平化的配置
                        result = {
                            'webui_port': config.get('webui_port', {}).get('default', 5000),
                            'backup_interval': config.get('backup_interval', {}).get('default', 24),
                            'backup_retention': config.get('backup_max_count', {}).get('default', 10)
                        }
                        logger.info(f"返回设置: {result}")
                        return jsonify(result)
                else:
                    logger.warning(f"配置文件不存在: {config_path}，使用默认配置")
                    # 返回默认配置
                    return jsonify({
                        'webui_port': 5000,
                        'backup_interval': 24,
                        'backup_retention': 10
                    })
            except Exception as e:
                logger.error(f"获取设置失败: {e}")
                import traceback
                logger.error(f"错误详情: {traceback.format_exc()}")
                return jsonify({
                    'webui_port': 5000,
                    'backup_interval': 24,
                    'backup_retention': 10
                }), 500

        @self.app.route('/api/settings', methods=['POST'])
        def api_save_settings(self):
            """保存系统设置"""
            try:
                data = request.json
                # 保存配置文件
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                import json
                
                # 读取现有配置
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        existing_config = json.load(f)
                else:
                    existing_config = {}
                
                # 只更新特定的配置项，保持配置文件格式不变
                if 'webui_port' in data:
                    if 'webui_port' in existing_config:
                        existing_config['webui_port']['default'] = data['webui_port']
                if 'backup_interval' in data:
                    if 'backup_interval' in existing_config:
                        existing_config['backup_interval']['default'] = data['backup_interval']
                if 'backup_retention' in data:
                    if 'backup_max_count' in existing_config:
                        existing_config['backup_max_count']['default'] = data['backup_retention']
                
                # 写回配置文件
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(existing_config, f, indent=2, ensure_ascii=False)
                return jsonify({'result': '设置保存成功'})
            except Exception as e:
                logger.error(f"保存设置失败: {e}")
                return jsonify({'result': f'保存失败: {e}'})

        @self.app.route('/api/backup')
        def api_create_backup():
            """创建备份"""
            try:
                result = self.db_manager.backup()
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"创建备份失败: {e}")
                return jsonify({'result': f'创建备份失败: {e}'})

        @self.app.route('/api/backups')
        def api_get_backups():
            """获取备份列表"""
            try:
                backups = self.db_manager.get_backup_list()
                return jsonify(backups)
            except Exception as e:
                logger.error(f"获取备份列表失败: {e}")
                return jsonify([])

        @self.app.route('/api/restore', methods=['POST'])
        def api_restore_backup():
            """从备份恢复"""
            try:
                data = request.json
                filename = data.get('filename')
                result = self.db_manager.restore_from_backup(filename)
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"恢复备份失败: {e}")
                return jsonify({'result': f'恢复失败: {e}'})

        @self.app.route('/api/cleanup')
        def api_cleanup_memories():
            """清理旧记忆"""
            try:
                result = self.db_manager.cleanup_memories()
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"清理记忆失败: {e}")
                return jsonify({'result': f'清理失败: {e}'})

        @self.app.route('/api/backup/<string:filename>', methods=['DELETE'])
        def api_delete_backup(filename):
            """删除备份"""
            try:
                # 这里需要实现删除备份的逻辑
                backup_dir = os.path.join(os.path.dirname(self.db_manager.db_path), "memory_capsule_backups")
                backup_path = os.path.join(backup_dir, filename)
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    return jsonify({'result': '删除成功'})
                else:
                    return jsonify({'result': '备份文件不存在'})
            except Exception as e:
                logger.error(f"删除备份失败: {e}")
                return jsonify({'result': f'删除失败: {e}'})

        @self.app.route('/api/activities')
        def api_get_activities():
            """获取最近活动"""
            limit = int(request.args.get('limit', 30))
            activities = self.db_manager.get_recent_activities(limit=limit)
            return jsonify(activities)

        @self.app.route('/shutdown', methods=['GET', 'POST'])
        def shutdown():
            """关闭服务器的内部接口"""
            shutdown_func = request.environ.get('werkzeug.server.shutdown')
            if shutdown_func is None:
                return 'Server shutdown not available'
            shutdown_func()
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
        """停止服务器"""
        if not self.running:
            logger.info("WebUI服务器未运行，无需停止。")
            return

        logger.info(f"正在停止 WebUI 服务器 (端口 {self.port})...")
        
        self.running = False
        
        try:
            import urllib.request
            urllib.request.urlopen(f'http://localhost:{self.port}/shutdown', timeout=2)
        except Exception:
            pass
        
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=3)
        
        logger.info(f"WebUI 服务器已停止，端口 {self.port} 已释放。")
