"""التشفير، كلمات المرور، والتحقق من توقيع Webhook."""
import hashlib
import hmac
import secrets

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from app.config import get_settings

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = get_settings().encryption_key
        if not key:
            raise RuntimeError("ENCRYPTION_KEY غير مضبوط. أنشئه بالأمر: python -m scripts.genkey")
        _fernet = Fernet(key.encode())
    return _fernet


def reset_crypto():
    global _fernet
    _fernet = None


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return "[تعذر فك التشفير]"


class EncryptedText(TypeDecorator):
    """عمود نصي مشفّر بـ Fernet (AES-128-CBC + HMAC) عند التخزين."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return decrypt(value) if value is not None else None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def new_api_key() -> tuple[str, str]:
    raw = "noof_" + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_meta_signature(body: bytes, signature_header: str | None) -> bool:
    """يتحقق من X-Hub-Signature-256 المرسل من Meta باستخدام App Secret."""
    secret = get_settings().wa_app_secret
    if not secret:
        # بدون App Secret لا نقبل أي طلب في الإنتاج
        return get_settings().app_env != "production"
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])
