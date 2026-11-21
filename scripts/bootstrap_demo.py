"""تهيئة بيئة التجربة (Render وغيرها): حساب مالك تجريبي + بيانات تجريبية موسومة. آمن للتكرار.
يتطلب DEMO_OWNER_PASSWORD. لا يعمل إذا كان WA_DRY_RUN=false (حماية من خلط التجربة بالإنتاج)."""
import os

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import Role, User
from app.nlp.arabic import normalize_phone
from app.security import hash_password
from scripts.seed_demo import seed


def main():
    pw = os.environ.get("DEMO_OWNER_PASSWORD")
    s = get_settings()
    if not pw or not s.wa_dry_run:
        print("bootstrap_demo: skipped")
        return
    phone = normalize_phone(s.owner_phone or "966500000099")
    with session_scope() as db:
        u = db.scalar(select(User).where(User.role == Role.owner))
        if u is None:
            u = User(name="المالك (تجربة)", phone=phone, role=Role.owner, is_demo=True)
            db.add(u)
        u.username, u.password_hash = "owner", hash_password(pw)
    seed()
    print("bootstrap_demo: ok")


if __name__ == "__main__":
    main()
