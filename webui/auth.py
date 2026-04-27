import os
import json
import secrets
import string
import time
from datetime import datetime, timedelta

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class AuthManager:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.auth_file = os.path.join(data_dir, "auth.json")
        self.sessions = {}
        self.session_timeout = 86400
        self.config = self._load_or_generate_token()

    def _load_or_generate_token(self):
        try:
            if os.path.exists(self.auth_file):
                with open(self.auth_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                expires_at = config.get('expires_at', '')
                if expires_at:
                    expires_dt = datetime.fromisoformat(expires_at)
                    if datetime.now() < expires_dt:
                        logger.info(f"WebUI token loaded from cache (expires {expires_at[:19].replace('T', ' ')})")
                        return config
        except Exception:
            pass
        return self._generate_new_token()

    def _generate_new_token(self):
        temp_token = self._generate_secure_token(length=32)
        token_expires = (datetime.now() + timedelta(hours=24)).isoformat()

        config = {
            "temp_token": temp_token,
            "token_generated_at": datetime.now().isoformat(),
            "expires_at": token_expires,
            "mode": "token_only"
        }

        try:
            os.makedirs(os.path.dirname(self.auth_file), exist_ok=True)
            with open(self.auth_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save token error: {e}")

        logger.info("=" * 50)
        logger.info("WebUI Auth Token (show once):")
        logger.info(f"  Token: {temp_token}")
        logger.info(f"  Expires: {token_expires[:19].replace('T', ' ')}")
        logger.info("=" * 50)

        return config

    def _generate_secure_token(self, length=32):
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
        token = ''.join(secrets.choice(alphabet) for _ in range(length))
        has_upper = any(c.isupper() for c in token)
        has_lower = any(c.islower() for c in token)
        has_digit = any(c.isdigit() for c in token)
        has_symbol = any(c in "!@#$%^&*-_=+" for c in token)
        if not (has_upper and has_lower and has_digit and has_symbol):
            return self._generate_secure_token(length)
        return token

    def authenticate(self, token):
        self._cleanup_sessions()
        current_token = self.config.get("temp_token", "")
        if token and secrets.compare_digest(token, current_token):
            session_token = self._create_session()
            return session_token
        return None

    def _create_session(self):
        session_token = secrets.token_urlsafe(32)
        self.sessions[session_token] = {
            "created_at": time.time(),
            "expires_in": self.session_timeout
        }
        return session_token

    def _cleanup_sessions(self):
        current_time = time.time()
        expired = [
            token for token, data in self.sessions.items()
            if current_time - data["created_at"] > data["expires_in"]
        ]
        for token in expired:
            del self.sessions[token]

    def validate_session(self, session_token):
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
        return {
            "mode": "token_only",
            "token_set": True,
            "active_sessions": len(self.sessions),
            "token_preview": self.config.get("temp_token", "")[:6] + "..." + self.config.get("temp_token", "")[-4:]
        }

    def regenerate_token(self):
        self.sessions.clear()
        self.config = self._generate_new_token()
        return {
            "success": True,
            "message": "New token generated, check logs"
        }
