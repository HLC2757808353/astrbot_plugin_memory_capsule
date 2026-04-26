from flask import Flask, render_template, jsonify, request, make_response, session, redirect, url_for
from functools import wraps
import threading
import time
import os
import json
import socket

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)

from .auth import AuthManager
from .version import get_plugin_version


class WebUIServer:
    def __init__(self, db_manager, host='0.0.0.0', port=5000, data_dir=None, existing_auth=None):
        self.app = Flask(__name__)
        self.app.secret_key = os.urandom(24).hex()
        self.db_manager = db_manager
        self.host = host
        self.port = port
        self.running = False
        self.version = get_plugin_version()
        self.server_thread = None
        self._server = None
        self._sock = None

        if existing_auth:
            self.auth_manager = existing_auth
        elif data_dir:
            self.auth_manager = AuthManager(data_dir)
        else:
            default_data_dir = os.path.dirname(self.db_manager.db_path) if self.db_manager else os.path.join(os.path.dirname(__file__), "..", "data")
            self.auth_manager = AuthManager(default_data_dir)

        self.public_routes = ['/login', '/api/login', '/api/auth/status']
        self.setup_routes()

    def _require_auth(self, f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.path in self.public_routes:
                return f(*args, **kwargs)
            session_token = request.cookies.get('session_token')
            if not session_token:
                session_token = request.headers.get('X-Session-Token')
            if not session_token or not self.auth_manager.validate_session(session_token):
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'error': 'unauthorized', 'code': 401}), 401
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated_function

    def setup_routes(self):

        @self.app.route('/login', methods=['GET', 'POST'])
        def login():
            if request.method == 'POST':
                data = request.json or request.form
                token = data.get('password', '')
                session_token = self.auth_manager.authenticate(token)
                if session_token:
                    response = jsonify({'success': True, 'message': 'OK'})
                    response.set_cookie('session_token', value=session_token,
                                       httponly=True, max_age=86400, samesite='Lax')
                    return response
                else:
                    return jsonify({'success': False, 'message': 'Wrong token'}), 401
            auth_status = self.auth_manager.get_status()
            response = make_response(render_template('login.html',
                                                     version=self.version, auth_status=auth_status))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/api/login', methods=['POST'])
        def api_login():
            data = request.json or {}
            token = data.get('password', '')
            session_token = self.auth_manager.authenticate(token)
            if session_token:
                response = jsonify({'success': True, 'message': 'OK'})
                response.set_cookie('session_token', value=session_token,
                                   httponly=True, max_age=86400)
                return response
            else:
                return jsonify({'success': False, 'message': 'Wrong token'}), 401

        @self.app.route('/api/logout', methods=['POST'])
        def api_logout():
            response = jsonify({'success': True})
            response.delete_cookie('session_token')
            return response

        @self.app.route('/api/auth/status')
        def api_auth_status():
            status = self.auth_manager.get_status()
            session_token = request.cookies.get('session_token') or request.headers.get('X-Session-Token')
            status['is_logged_in'] = bool(session_token and self.auth_manager.validate_session(session_token))
            return jsonify(status)

        @self.app.route('/api/auth/reset', methods=['POST'])
        def api_reset_auth():
            result = self.auth_manager.regenerate_token()
            return jsonify(result)

        @self.app.route('/')
        @self._require_auth
        def index():
            response = make_response(render_template('index.html', version=self.version))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/memories')
        @self._require_auth
        def memories():
            response = make_response(render_template('memories.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/relationships')
        @self._require_auth
        def relationships():
            response = make_response(render_template('relationships.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/settings')
        @self._require_auth
        def settings():
            response = make_response(render_template('settings.html',
                                                     auth_status=self.auth_manager.get_status()))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

        @self.app.route('/api/memories')
        @self._require_auth
        def api_memories():
            page = int(request.args.get('page', 1))
            limit = int(request.args.get('limit', 10))
            offset = (page - 1) * limit
            category = request.args.get('category')
            memories = self.db_manager.get_all_memories(limit, offset, category)
            total_memories = self.db_manager.get_memories_count()
            return jsonify({
                'memories': memories, 'total': total_memories, 'page': page,
                'limit': limit, 'total_pages': (total_memories + limit - 1) // limit
            })

        @self.app.route('/api/relationships')
        @self._require_auth
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
        @self._require_auth
        def api_add_memory():
            data = request.json
            result = self.db_manager.write_memory(
                content=data.get('content', ''),
                category=data.get('category'),
                importance=data.get('importance', 5)
            )
            return jsonify({'result': result})

        @self.app.route('/api/memories/<int:memory_id>', methods=['DELETE'])
        @self._require_auth
        def api_delete_memory(memory_id):
            result = self.db_manager.delete_memory(memory_id)
            return jsonify({'result': result})

        @self.app.route('/api/memories/<int:memory_id>', methods=['PUT'])
        @self._require_auth
        def api_update_memory(memory_id):
            data = request.json
            result = self.db_manager.update_memory(
                memory_id=memory_id,
                content=data.get('content'),
                category=data.get('category'),
                importance=data.get('importance'),
                tags=data.get('tags')
            )
            return jsonify({'result': result})

        @self.app.route('/api/memories/search')
        @self._require_auth
        def api_search_memories():
            query = request.args.get('q', '')
            category = request.args.get('category')
            limit = request.args.get('limit', type=int)
            memories = self.db_manager.search_memory(query, category_filter=category, limit=limit)
            return jsonify(memories)

        @self.app.route('/api/tags')
        @self._require_auth
        def api_tags():
            tags = self.db_manager.get_all_tags()
            return jsonify(tags)

        @self.app.route('/api/categories')
        @self._require_auth
        def api_categories():
            categories = self.db_manager.get_memory_categories()
            return jsonify(categories)

        @self.app.route('/api/relationships', methods=['POST'])
        @self._require_auth
        def api_add_relationship():
            data = request.json
            result = self.db_manager.update_relationship_enhanced(
                user_id=data.get('user_id', ''),
                relation_type=data.get('relation_type'),
                summary=data.get('summary'),
                nickname=data.get('nickname'),
                first_met_location=data.get('first_met_location'),
                known_contexts=data.get('known_contexts')
            )
            return jsonify({'result': result})

        @self.app.route('/api/relationships/<string:user_id>', methods=['DELETE'])
        @self._require_auth
        def api_delete_relationship(user_id):
            result = self.db_manager.delete_relationship(user_id)
            return jsonify({'result': result})

        @self.app.route('/api/relationships/search')
        @self._require_auth
        def api_search_relationships():
            query = request.args.get('q', '')
            results = self.db_manager.search_relationship(query, limit=10)
            return jsonify(results)

        @self.app.route('/api/settings', methods=['GET'])
        @self._require_auth
        def api_get_settings():
            try:
                cfg = self.db_manager.config if self.db_manager else {}
                schema_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                schema = {}
                if os.path.exists(schema_path):
                    with open(schema_path, 'r', encoding='utf-8') as f:
                        schema = json.load(f)
                result = {}
                for key, meta in schema.items():
                    if meta.get('editable', False):
                        result[key] = {
                            'value': cfg.get(key, meta.get('default')),
                            'type': meta.get('type', 'string'),
                            'display_name': meta.get('display_name', key),
                            'hint': meta.get('hint', ''),
                            'default': meta.get('default'),
                            'options': meta.get('options')
                        }
                result['auth_status'] = self.auth_manager.get_status()
                return jsonify(result)
            except Exception as e:
                logger.error(f"Get settings error: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/settings', methods=['POST'])
        @self._require_auth
        def api_save_settings():
            try:
                data = request.json
                schema_path = os.path.join(os.path.dirname(__file__), "..", "_conf_schema.json")
                schema = {}
                if os.path.exists(schema_path):
                    with open(schema_path, 'r', encoding='utf-8') as f:
                        schema = json.load(f)
                if self.db_manager and self.db_manager.config is not None:
                    for key, value in data.items():
                        meta = schema.get(key, {})
                        field_type = meta.get('type', 'string')
                        if field_type == 'list' and isinstance(value, str):
                            value = [v.strip() for v in value.split(',') if v.strip()]
                        elif field_type == 'int' and isinstance(value, (float, str)):
                            value = int(float(value))
                        elif field_type == 'float' and isinstance(value, str):
                            value = float(value)
                        elif field_type == 'bool' and isinstance(value, str):
                            value = value.lower() in ('true', '1', 'yes')
                        self.db_manager.config[key] = value
                config_path = os.path.join(os.path.dirname(self.db_manager.db_path), "runtime_config.json")
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(self.db_manager.config if self.db_manager else data, f, ensure_ascii=False)
                return jsonify({'result': 'Saved (some need restart)'})
            except Exception as e:
                logger.error(f"Save settings error: {e}")
                return jsonify({'result': f'Save failed: {e}'})

        @self.app.route('/api/backup')
        @self._require_auth
        def api_create_backup():
            try:
                result = self.db_manager.backup()
                return jsonify({'result': result})
            except Exception as e:
                return jsonify({'result': f'Backup failed: {e}'})

        @self.app.route('/api/backups')
        @self._require_auth
        def api_get_backups():
            try:
                backups = self.db_manager.get_backup_list()
                return jsonify(backups)
            except Exception:
                return jsonify([])

        @self.app.route('/api/restore', methods=['POST'])
        @self._require_auth
        def api_restore_backup():
            try:
                data = request.json
                filename = data.get('filename')
                result = self.db_manager.restore_from_backup(filename)
                return jsonify({'result': result})
            except Exception as e:
                return jsonify({'result': f'Restore failed: {e}'})

        @self.app.route('/api/cleanup')
        @self._require_auth
        def api_cleanup_memories():
            try:
                result = self.db_manager.cleanup_memories()
                return jsonify({'result': result})
            except Exception as e:
                return jsonify({'result': f'Cleanup failed: {e}'})

        @self.app.route('/api/backup/<string:filename>', methods=['DELETE'])
        @self._require_auth
        def api_delete_backup(filename):
            try:
                backup_dir = os.path.join(os.path.dirname(self.db_manager.db_path), "memory_capsule_backups")
                backup_path = os.path.join(backup_dir, filename)
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                    return jsonify({'result': 'Deleted'})
                else:
                    return jsonify({'result': 'Not found'})
            except Exception as e:
                return jsonify({'result': f'Delete failed: {e}'})

        @self.app.route('/api/activities')
        @self._require_auth
        def api_get_activities():
            limit = int(request.args.get('limit', 30))
            activities = self.db_manager.get_recent_activities(limit=limit)
            return jsonify(activities)

        @self.app.route('/api/import', methods=['POST'])
        @self._require_auth
        def api_bulk_import():
            try:
                data = request.json
                items = data.get('memories', [])
                if not items or not isinstance(items, list):
                    return jsonify({'result': 'No memories array provided'}), 400
                if len(items) > 500:
                    return jsonify({'result': f'Too many items ({len(items)}), max 500 per batch'}), 400
                result = self.db_manager.bulk_import_memories(items)
                return jsonify({'result': result})
            except Exception as e:
                logger.error(f"Bulk import error: {e}")
                return jsonify({'result': f'Import failed: {e}'}), 500

        @self.app.route('/api/stats')
        @self._require_auth
        def api_stats():
            try:
                stats = self.db_manager.get_memory_stats()
                return jsonify(stats)
            except Exception as e:
                return jsonify({'error': str(e)}), 500

    def run(self):
        self.running = True
        try:
            from werkzeug.serving import make_server
            for attempt in range(5):
                try:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self._sock.bind((self.host, self.port))
                    self._sock.listen(5)
                    break
                except OSError as e:
                    self._cleanup_socket()
                    if attempt < 4:
                        time.sleep(1)
                    else:
                        logger.error(f"WebUI {self.host}:{self.port} still occupied after 5 retries")
                        self.running = False
                        return
            self._server = make_server(self.host, self.port, self.app, threaded=True, fd=self._sock.fileno())
            logger.info(f"WebUI started on {self.host}:{self.port}")
            self._server.serve_forever()
        except OSError as e:
            logger.error(f"WebUI port {self.port} error: {e}")
        except Exception as e:
            if "KeyboardInterrupt" not in str(e):
                logger.error(f"WebUI error: {e}")
        finally:
            self._cleanup_socket()
            self.running = False

    def _cleanup_socket(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        self._cleanup_socket()
        self.running = False
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
