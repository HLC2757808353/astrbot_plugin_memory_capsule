import os
import json
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
    """WebUI认证管理器（纯Token模式）
    
    简化版：只使用临时Token，不设置永久密码
    - 每次启动生成新的随机Token
    - Token仅在启动时打印到日志一次
    - 忘记Token只需重启插件
    - Session有效期24小时
    """
    
    def __init__(self, data_dir):
        """
        参数：
        - data_dir: 数据目录路径（用于存储配置）
        """
        self.data_dir = data_dir
        self.auth_file = os.path.join(data_dir, "auth.json")
        
        # 会话存储 {session_token: {"created_at": timestamp, "expires_in": 秒}}
        self.sessions = {}
        
        # Session过期时间（默认24小时）
        self.session_timeout = 86400
        
        # 生成新的Token（每次实例化都重新生成）
        self.config = self._generate_new_token()
    
    def _generate_new_token(self):
        """生成全新的临时Token"""
        temp_token = self._generate_temp_token(length=8)
        token_expires = (datetime.now() + timedelta(hours=24)).isoformat()
        
        config = {
            "temp_token": temp_token,
            "token_generated_at": datetime.now().isoformat(),
            "expires_at": token_expires,
            "mode": "token_only"  # 标记为纯Token模式
        }
        
        try:
            os.makedirs(os.path.dirname(self.auth_file), exist_ok=True)
            with open(self.auth_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存Token配置失败: {e}")
        
        # 打印重要日志（只打印一次）
        logger.info("=" * 60)
        logger.info("🔐 WebUI 认证信息")
        logger.info("-" * 60)
        logger.info(f"   📱 访问地址: http://localhost:{self._get_webui_port()}")
        logger.info(f"   🔑 登录Token: {temp_token}")
        logger.info(f"   ⏰ 有效期至: {token_expires[:19].replace('T', ' ')}")
        logger.info("-" * 60)
        logger.info("💡 提示: Token仅在此显示一次！")
        logger.info("   如忘记Token，请重启插件查看新Token")
        logger.info("=" * 60)
        
        return config
    
    def _get_webui_port(self):
        """尝试获取WebUI端口"""
        try:
            conf_path = os.path.join(os.path.dirname(__file__), "..", "..", "_conf_schema.json")
            if os.path.exists(conf_path):
                with open(conf_path, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
                    return conf.get('webui_port', {}).get('default', 5000)
        except:
            pass
        return 5000
    
    def _generate_temp_token(self, length=8):
        """生成随机Token"""
        import string
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    def authenticate(self, token):
        """验证用户输入的Token
        
        返回值：
        - 成功: session_token (字符串)
        - 失败: None
        """
        # 清理过期会话
        self._cleanup_sessions()
        
        current_token = self.config.get("temp_token", "")
        
        if token == current_token:
            session_token = self._create_session()
            logger.info("✅ 用户通过Token成功登录WebUI")
            return session_token
        
        logger.warning("❌ Token验证失败")
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
        """验证会话是否有效"""
        if not session_token:
            return False
        
        self._cleanup_sessions()
        
        session_data = self.sessions.get(session_token)
        if not session_data:
            return False
        
        if time.time() - session_data["created_at"] > session_data["expires_in"]:
            del self.sessions[session_token]
            return False
        
        return True
    
    def get_status(self):
        """获取认证状态信息（用于API）"""
        return {
            "mode": "token_only",
            "token_set": True,
            "active_sessions": len(self.sessions),
            "config_file": self.auth_file,
            "current_token_prefix": self.config.get("temp_token", "")[:4] + "****"  # 只显示前4位
        }
    
    def regenerate_token(self):
        """重新生成Token（用于忘记密码时）"""
        self.sessions.clear()  # 清除所有会话
        self.config = self._generate_new_token()
        
        return {
            "success": True,
            "message": "已生成新Token，请查看日志",
            "token_prefix": self.config["temp_token"][:4] + "****"
        }
