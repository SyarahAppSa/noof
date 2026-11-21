"""يولّد مفتاح تشفير ومفتاح جلسات جديدين."""
import secrets

from cryptography.fernet import Fernet

print("ENCRYPTION_KEY=" + Fernet.generate_key().decode())
print("SECRET_KEY=" + secrets.token_urlsafe(48))
print("WA_VERIFY_TOKEN=" + secrets.token_urlsafe(24))
