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
                category=data.get('category', '日常'),
                tags=data.get('tags', ''),
                target_user_id=data.get('target_user_id'),
                source_platform=data.get('source_platform', 'Web'),
                source_context=data.get('source_context', ''),
                importance=data.get('importance', 5)
            )
            return jsonify({'result': result})

        @self.app.route('/api/memories/<memory_id>', methods=['DELETE'])
        def api_delete_memory(memory_id):
            result = self.db_manager.delete_memory(memory_id)
            return jsonify({'result': result})

        @self.app.route('/api/memories/search')
        def api_search_memories():
            """搜索记忆"""
            query = request.args.get('q')
            target_user_id = request.args.get('target_user_id')
            memories = self.db_manager.search_memory(query, target_user_id)
            return jsonify(memories)
        
        @self.app.route('/api/tags')
        def api_tags():
            """获取所有标签"""
            tags = self.db_manager.get_all_tags()
            return jsonify(tags)
        
        @self.app.route('/api/categories')
        def api_categories():
            """获取所有分类"""
            categories = self.db_manager.get_all_categories()
            return jsonify(categories)

        @self.app.route('/api/relationships', methods=['POST'])
        def api_add_relationship():
            data = request.json
            result = self.db_manager.update_relationship(
                user_id=data.get('user_id', ''),
                relation_type=data.get('relation_type', ''),
                tags_update=data.get('tags_update', ''),
                summary_update=data.get('summary_update', ''),
                intimacy_change=data.get('intimacy_change', 0),
                nickname=data.get('nickname', ''),
                first_met_time=data.get('first_met_time'),
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
                cursor.execute('SELECT * FROM relationships WHERE nickname LIKE ? OR relation_type LIKE ? OR tags LIKE ? OR summary LIKE ? OR first_met_location LIKE ? OR known_contexts LIKE ?', 
                              (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%"))
                results = cursor.fetchall()
            
            conn.close()
            relationship_list = []
            for r in results:
                relationship = {
                    "user_id": r[0],
                    "nickname": r[1],
                    "relation_type": r[2],
                    "intimacy": r[3],
                    "tags": r[4],
                    "summary": r[5],
                    "first_met_time": r[6],
                    "first_met_location": r[7],
                    "known_contexts": r[8],
                    "updated_at": r[9]
                }
                relationship_list.append(relationship)
            return jsonify(relationship_list)

        @self.app.route('/api/settings', methods=['GET'])
        def api_get_settings():
            """获取系统设置"""
            try:
                # 读取配置文件
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        import json
                        config = json.load(f)
                        return jsonify(config)
                else:
                    # 返回默认配置
                    return jsonify({
                        'webui_port': 5000,
                        'backup_interval': 24,
                        'backup_retention': 10
                    })
            except Exception as e:
                logger.error(f"获取设置失败: {e}")
                return jsonify({
                    'webui_port': 5000,
                    'backup_interval': 24,
                    'backup_retention': 10
                })

        @self.app.route('/api/settings', methods=['POST'])
        def api_save_settings():
            """保存系统设置"""
            try:
                data = request.json
                # 保存配置文件
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                import json
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
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
