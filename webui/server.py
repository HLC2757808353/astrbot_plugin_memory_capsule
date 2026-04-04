from flask import Flask, render_template, jsonify, request, make_response, session, redirect, url_for
from functools import wraps
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

from .auth import AuthManager


class WebUIServer:
    def __init__(self, db_manager, port=5000, data_dir=None):
        self.app = Flask(__name__)
        self.app.secret_key = os.urandom(24).hex()  # 用于Session签名
        
        self.db_manager = db_manager
        self.port = port
        self.running = False
        self.version = self._get_version()
        self.server_thread = None
        self._own_pid = os.getpid()
        
        # 初始化认证管理器
        if data_dir:
            self.auth_manager = AuthManager(data_dir)
        else:
            # 默认使用插件data目录
            default_data_dir = os.path.join(os.path.dirname(__file__), "..", "..", "data")
            self.auth_manager = AuthManager(default_data_dir)
        
        # 不需要认证的路由（公开访问）
        self.public_routes = ['/login', '/api/login', '/api/auth/status']
        
        self.setup_routes()
    
    def _get_version(self):
        """从metadata.yaml读取版本号"""
        try:
            metadata_path = os.path.join(os.path.dirname(__file__), "..", "metadata.yaml")
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = yaml.safe_load(f)
                return metadata.get('version', 'v0.9.5')
        except Exception as e:
            logger.error(f"读取版本号失败: {e}")
            return 'v0.9.5'
    
    def _require_auth(self, f):
        """认证装饰器 - 验证用户是否已登录"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 检查是否是公开路由
            if request.path in self.public_routes:
                return f(*args, **kwargs)
            
            # 从Cookie或Header获取session token
            session_token = request.cookies.get('session_token')
            if not session_token:
                session_token = request.headers.get('X-Session-Token')
            
            # 验证session
            if not session_token or not self.auth_manager.validate_session(session_token):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'error': '未授权', 'code': 401}), 401
                return redirect(url_for('login'))
            
            return f(*args, **kwargs)
        return decorated_function
    
    def setup_routes(self):
        """设置路由"""
        
        # ====== 公开路由（无需认证）======
        
        @self.app.route('/login', methods=['GET', 'POST'])
        def login():
            """登录页面"""
            if request.method == 'POST':
                data = request.json or request.form
                password = data.get('password', '')
                
                session_token = self.auth_manager.authenticate(password)
                
                if session_token:
                    response = jsonify({
                        'success': True,
                        'message': '登录成功',
                        'redirect': '/',
                        'password_set': self.auth_manager.is_password_set()
                    })
                    response.set_cookie(
                        'session_token',
                        value=session_token,
                        httponly=True,
                        max_age=86400,  # 24小时
                        samesite='Lax'
                    )
                    return response
                else:
                    return jsonify({
                        'success': False,
                        'message': '密码错误或Token已过期',
                        'password_set': self.auth_manager.is_password_set()
                    }), 401
            
            # GET请求返回登录页面
            auth_status = self.auth_manager.get_status()
            response = make_response(render_template('login.html', 
                                                   version=self.version,
                                                   auth_status=auth_status))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response
        
        @self.app.route('/api/login', methods=['POST'])
        def api_login():
            """API登录接口"""
            data = request.json or {}
            password = data.get('password', '')
            
            session_token = self.auth_manager.authenticate(password)
            
            if session_token:
                response = jsonify({
                    'success': True,
                    'message': '登录成功',
                    'password_set': self.auth_manager.is_password_set()
                })
                response.set_cookie(
                    'session_token',
                    value=session_token,
                    httponly=True,
                    max_age=86400
                )
                return response
            else:
                return jsonify({
                    'success': False,
                    'message': '密码错误',
                    'password_set': self.auth_manager.is_password_set()
                }), 401
        
        @self.app.route('/api/logout', methods=['POST'])
        def api_logout():
            """登出接口"""
            response = jsonify({'success': True, 'message': '已登出'})
            response.delete_cookie('session_token')
            return response
        
        @self.app.route('/api/auth/status')
        def api_auth_status():
            """获取认证状态（公开接口）"""
            status = self.auth_manager.get_status()
            
            # 检查当前是否已登录
            session_token = request.cookies.get('session_token') or request.headers.get('X-Session-Token')
            is_logged_in = bool(session_token and self.auth_manager.validate_session(session_token))
            
            status['is_logged_in'] = is_logged_in
            return jsonify(status)
        
        @self.app.route('/api/auth/password', methods=['POST'])
        def api_set_password():
            """设置新密码（需要已登录）"""
            session_token = request.cookies.get('session_token') or request.headers.get('X-Session-Token')
            
            if not session_token or not self.auth_manager.validate_session(session_token):
                return jsonify({'success': False, 'message': '未授权'}), 401
            
            data = request.json or {}
            new_password = data.get('new_password', '')
            
            if self.auth_manager.set_password(new_password):
                # 密码设置成功，更新session
                response = jsonify({
                    'success': True,
                    'message': '密码设置成功，请重新登录'
                })
                response.delete_cookie('session_token')
                return response
            else:
                return jsonify({
                    'success': False,
                    'message': '密码不符合要求（4-64个字符）'
                }), 400
        
        @self.app.route('/api/auth/reset', methods=['POST'])
        def api_reset_auth():
            """重置认证系统（危险操作，仅用于忘记密码时）
            
            注意：这会删除所有密码配置，生成新的临时Token
            """
            result = self.auth_manager.reset_auth()
            return jsonify(result)
        
        # ====== 需要认证的路由 ======
        
        @self.app.route('/')
        @_require_auth(self)
        def index():
            response = make_response(render_template('index.html', version=self.version))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/notes')
        @_require_auth(self)
        def notes():
            response = make_response(render_template('notes.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/memories')
        @_require_auth(self)
        def memories():
            response = make_response(render_template('memories.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/relationships')
        @_require_auth(self)
        def relationships():
            response = make_response(render_template('relationships.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/settings')
        @_require_auth(self)
        def settings():
            response = make_response(render_template('settings.html',
                                                    auth_status=self.auth_manager.get_status()))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        # ====== API 路由（需要认证）======
        
        @self.app.route('/api/memories')
        @_require_auth(self)
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
        @_require_auth(self)
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
        @_require_auth(self)
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
        @_require_auth(self)
        def api_delete_memory(memory_id):
            result = self.db_manager.delete_memory(memory_id)
            return jsonify({'result': result})
        
        @self.app.route('/api/memories/<memory_id>', methods=['PUT'])
        @_require_auth(self)
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
        @_require_auth(self)
        def api_search_memories():
            """搜索记忆"""
            query = request.args.get('q', '')
            category = request.args.get('category')
            limit = request.args.get('limit', type=int)
            memories = self.db_manager.search_memory(query, category_filter=category, limit=limit)
            return jsonify(memories)
        
        @self.app.route('/api/tags')
        @_require_auth(self)
        def api_tags():
            """获取所有标签"""
            tags = self.db_manager.get_all_tags()
            return jsonify(tags)
        
        @self.app.route('/api/categories')
        @_require_auth(self)
        def api_categories():
            """获取所有分类"""
            categories = self.db_manager.get_memory_categories()
            return jsonify(categories)

        @self.app.route('/api/relationships', methods=['POST'])
        @_require_auth(self)
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
        @_require_auth(self)
        def api_delete_relationship(user_id):
            result = self.db_manager.delete_relationship(user_id)
            return jsonify({'result': result})

        @self.app.route('/api/relationships/search')
        @_require_auth(self)
        def api_search_relationships():
            query = request.args.get('q', '')
            results = self.db_manager.search_relationship(query, limit=10)
            return jsonify(results)

        @self.app.route('/api/settings', methods=['GET'])
        @_require_auth(self)
        def api_get_settings(self):
            """获取系统设置"""
            try:
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        import json
                        config = json.load(f)
                        result = {
                            'webui_port': config.get('webui_port', {}).get('default', 5000),
                            'backup_interval': config.get('backup_interval', {}).get('default', 24),
                            'backup_retention': config.get('backup_max_count', {}).get('default', 10),
                            'auth_status': self.auth_manager.get_status()
                        }
                        return jsonify(result)
                else:
                    return jsonify({
                        'webui_port': 5000,
                        'backup_interval': 24,
                        'backup_retention': 10,
                        'auth_status': self.auth_manager.get_status()
                    })
            except Exception as e:
                logger.error(f"获取设置失败: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/settings', methods=['POST'])
        @_require_auth(self)
        def api_save_settings(self):
            """保存系统设置"""
            try:
                data = request.json
                config_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                import json
                
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        existing_config = json.load(f)
                else:
                    existing_config = {}
                
                if 'webui_port' in data:
                    if 'webui_port' in existing_config:
                        existing_config['webui_port']['default'] = data['webui_port']
                if 'backup_interval' in data:
                    if 'backup_interval' in existing_config:
                        existing_config['backup_interval']['default'] = data['backup_interval']
                if 'backup_retention' in data:
                    if 'backup_max_count' in existing_config:
                        existing_config['backup_max_count']['default'] = data['backup_retention']
                
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(existing_config, f, indent=2, ensure_ascii=False)
                return jsonify({'result': '设置保存成功'})
            except Exception as e:
                logger.error(f"保存设置失败: {e}")
                return jsonify({'result': f'保存失败: {e}'})

        @self.app.route('/api/backup')
        @_require_auth(self)
        def api_create_backup():
            """创建备份"""
            try:
                result = self.db_manager.backup()
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"创建备份失败: {e}")
                return jsonify({'result': f'创建备份失败: {e}'})

        @self.app.route('/api/backups')
        @_require_auth(self)
        def api_get_backups():
            """获取备份列表"""
            try:
                backups = self.db_manager.get_backup_list()
                return jsonify(backups)
            except Exception as e:
                logger.error(f"获取备份列表失败: {e}")
                return jsonify([])

        @self.app.route('/api/restore', methods=['POST'])
        @_require_auth(self)
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
        @_require_auth(self)
        def api_cleanup_memories():
            """清理旧记忆"""
            try:
                result = self.db_manager.cleanup_memories()
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"清理记忆失败: {e}")
                return jsonify({'result': f'清理失败: {e}'})

        @self.app.route('/api/backup/<string:filename>', methods=['DELETE'])
        @_require_auth(self)
        def api_delete_backup(filename):
            """删除备份"""
            try:
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
        @_require_auth(self)
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
            logger.info(f"WebUI服务启动中... (端口: {self.port})")
            logger.info(f"访问地址: http://localhost:{self.port}")
            
            if not self.auth_manager.is_password_set():
                logger.warning("⚠️  尚未设置登录密码！请查看上方日志中的临时Token")
            
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
