import os
import json
import hashlib
import secrets
import time
from datetime import datetime, timedelta

# 容错处理
try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)


class AuthManager:
    """WebUI认证管理器
    
    功能：
    - 首次启动生成临时Token
    - 支持用户设置自定义密码
    - 密码哈希存储（不存明文）
    - Session会话管理
    """
    
    def __init__(self, data_dir):
        """
        参数：
        - data_dir: 数据目录路径（用于存储 auth.json）
        """
        self.data_dir = data_dir
        self.auth_file = os.path.join(data_dir, "auth.json")
        
        # 会话存储 {session_token: {"created_at": timestamp, "expires_in": 秒}}
        self.sessions = {}
        
        # Session过期时间（默认24小时）
        self.session_timeout = 86400
        
        # 加载或初始化认证配置
        self.config = self._load_or_init_auth()
    
    def _load_or_init_auth(self):
        """加载现有认证配置，如果不存在则初始化"""
        if os.path.exists(self.auth_file):
            try:
                with open(self.auth_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                logger.info(f"已加载认证配置: {self.auth_file}")
                return config
            except Exception as e:
                logger.error(f"加载认证配置失败: {e}，将重新生成")
        
        # 生成新的临时Token
        return self._generate_new_config()
    
    def _generate_new_config(self):
        """生成全新的认证配置"""
        temp_token = self._generate_temp_token()
        token_expires = (datetime.now() + timedelta(hours=24)).isoformat()
        
        config = {
            "temp_token": temp_token,
            "temp_token_expires": token_expires,
            "password_hash": None,  # 用户还没设置密码
            "password_set": False,
            "created_at": datetime.now().isoformat()
        }
        
        # 保存到文件
        self._save_config(config)
        
        # 打印重要提示到日志
        logger.info("=" * 60)
        logger.info("⚠️  WebUI 首次启动！请使用以下临时密码登录：")
        logger.info(f"   临时密码: {temp_token}")
        logger.info(f"   有效期至: {token_expires}")
        logger.info("   登录后请立即在设置页面修改密码！")
        logger.info("=" * 60)
        
        return config
    
    def _generate_temp_token(self, length=8):
        """生成随机临时Token"""
        import string
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    def _save_config(self, config):
        """保存认证配置到文件"""
        try:
            os.makedirs(os.path.dirname(self.auth_file), exist_ok=True)
            with open(self.auth_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存认证配置失败: {e}")
    
    def _hash_password(self, password):
        """对密码进行哈希（使用SHA256 + salt）"""
        salt = secrets.token_hex(16)
        password_bytes = password.encode('utf-8')
        salt_bytes = salt.encode('utf-8')
        
        hashed = hashlib.sha256(password_bytes + salt_bytes).hexdigest()
        return f"{salt}:{hashed}"
    
    def _verify_password(self, password, stored_hash):
        """验证密码"""
        try:
            salt, hashed = stored_hash.split(':')
            password_bytes = password.encode('utf-8')
            salt_bytes = salt.encode('utf-8')
            
            new_hash = hashlib.sha256(password_bytes + salt_bytes).hexdigest()
            return new_hash == hashed
        except Exception:
            return False
    
    def authenticate(self, password):
        """验证用户输入的密码
        
        返回值：
        - 成功: session_token (字符串)
        - 失败: None
        """
        # 清理过期会话
        self._cleanup_sessions()
        
        # 检查是否是临时Token
        if not self.config.get("password_set", False):
            # 还没设置密码，只能用临时Token登录
            temp_token = self.config.get("temp_token", "")
            token_expires = self.config.get("temp_token_expires", "")
            
            if password == temp_token:
                # 检查Token是否过期
                try:
                    expires_time = datetime.fromisoformat(token_expires)
                    if datetime.now() > expires_time:
                        logger.warning("临时Token已过期，正在生成新Token...")
                        self.config = self._generate_new_config()
                        return None
                except Exception:
                    pass
                
                # 创建会话
                session_token = self._create_session()
                logger.info("用户通过临时Token成功登录")
                return session_token
            
            logger.warning("临时Token错误")
            return None
        else:
            # 已设置密码，验证密码
            password_hash = self.config.get("password_hash")
            if password_hash and self._verify_password(password, password_hash):
                session_token = self._create_session()
                logger.info("用户通过密码成功登录")
                return session_token
            
            logger.warning("密码错误")
            return None
    
    def _create_session(self):
        """创建新会话"""
        session_token = secrets.token_urlsafe(32)
        self.sessions[session_token] = {
            "created_at": time.time(),
            "expires_in": self.session_timeout
        }
        return session_token
    
    def _cleanup_sessions(self):
        """清理过期会话"""
        current_time = time.time()
        expired = [
            token for token, data in self.sessions.items()
            if current_time - data["created_at"] > data["expires_in"]
        ]
        for token in expired:
            del self.sessions[token]
    
    def validate_session(self, session_token):
        """验证会话是否有效
        
        返回值：
        - True: 会话有效
        - False: 会话无效或过期
        """
        if not session_token:
            return False
        
        self._cleanup_sessions()
        
        session_data = self.sessions.get(session_token)
        if not session_data:
            return False
        
        # 检查是否过期
        if time.time() - session_data["created_at"] > session_data["expires_in"]:
            del self.sessions[session_token]
            return False
        
        return True
    
    def set_password(self, new_password):
        """设置新密码
        
        参数：
        - new_password: 新密码（明文）
        
        返回值：
        - True: 设置成功
        - False: 密码不符合要求
        """
        # 密码长度验证
        if len(new_password) < 4:
            logger.warning("密码太短，至少需要4个字符")
            return False
        
        if len(new_password) > 64:
            logger.warning("密码太长，最多64个字符")
            return False
        
        # 哈希并保存
        password_hash = self._hash_password(new_password)
        self.config["password_hash"] = password_hash
        self.config["password_set"] = True
        self.config["password_changed_at"] = datetime.now().isoformat()
        
        # 使旧临时Token失效
        self.config["temp_token"] = None
        self.config["temp_token_expires"] = None
        
        self._save_config(self.config)
        
        # 清除所有现有会话（强制重新登录）
        self.sessions.clear()
        
        logger.info("用户密码已成功更新")
        return True
    
    def is_password_set(self):
        """检查是否已设置密码"""
        return self.config.get("password_set", False)
    
    def get_status(self):
        """获取认证状态信息（用于API）"""
        return {
            "password_set": self.config.get("password_set", False),
            "has_temp_token": self.config.get("temp_token") is not None,
            "active_sessions": len(self.sessions),
            "config_file": self.auth_file
        }
    
    def reset_auth(self):
        """重置认证系统（删除所有配置，重新生成）
        
        用途：忘记密码时重置
        """
        if os.path.exists(self.auth_file):
            os.remove(self.auth_file)
            logger.info("认证配置已删除")
        
        self.sessions.clear()
        self.config = self._generate_new_config()
        
        return {
            "success": True,
            "message": "认证系统已重置，请查看日志获取新的临时密码"
        }
