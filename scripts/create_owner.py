"""ينشئ حساب المالك (أو يحدّثه) + كلمة مرور لوحة التحكم + مفتاح API.

python -m scripts.create_owner --name "الاسم" --phone 05XXXXXXXX --username owner
"""
import argparse
import getpass

from sqlalchemy import select

from app.audit import audit
from app.db import session_scope
from app.models import ApiKey, Role, User
from app.nlp.arabic import normalize_phone
from app.security import hash_password, new_api_key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--phone", required=True)
    ap.add_argument("--username", default="owner")
    ap.add_argument("--company", default=None)
    a = ap.parse_args()
    pw = getpass.getpass("كلمة مرور لوحة التحكم (12 حرفًا على الأقل): ")
    if len(pw) < 12:
        raise SystemExit("كلمة المرور قصيرة")
    phone = normalize_phone(a.phone)
    with session_scope() as db:
        u = db.scalar(select(User).where(User.phone == phone))
        if u is None:
            u = User(name=a.name, phone=phone, role=Role.owner)
            db.add(u)
        u.name, u.role, u.username, u.password_hash, u.is_active = a.name, Role.owner, a.username, hash_password(pw), True
        db.flush()
        raw, h = new_api_key()
        db.add(ApiKey(user_id=u.id, name="owner-cli", key_hash=h))
        if a.company:
            from app.services.settings import set_setting
            set_setting(db, "company_name", a.company)
        audit(db, "owner.created", actor_type="system", entity_type="user", entity_id=u.id)
    print(f"✅ تم إعداد المالك ({phone}). اسم المستخدم: {a.username}")
    print(f"مفتاح API (يظهر مرة واحدة): {raw}")


if __name__ == "__main__":
    main()
