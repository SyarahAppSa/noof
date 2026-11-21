"""بيئة الاختبار: قاعدة PostgreSQL حقيقية (noof_test) + عميل واتساب وهمي يسجل كل ما يُرسل.
كل البيانات هنا تجريبية."""
import hashlib
import hmac
import itertools
import json
import os
from datetime import datetime

from cryptography.fernet import Fernet

os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://noof:noof@localhost:5432/noof_test"),
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "SECRET_KEY": "test-secret",
    "OWNER_PHONE": "966500000001",
    "WA_PHONE_NUMBER_ID": "PN_NOOF",
    "WA_ACCESS_TOKEN": "test",
    "WA_DRY_RUN": "false",
    "WA_APP_SECRET": "app-secret",
    "WA_VERIFY_TOKEN": "verify-me",
    "ANTHROPIC_API_KEY": "",
})

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import TZ  # noqa: E402
from app.db import SessionLocal, get_engine  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.timeutil import set_now  # noqa: E402
from app.whatsapp.client import SendResult, set_client_override  # noqa: E402

SUNDAY_9AM = datetime(2026, 11, 22, 9, 0, tzinfo=TZ)


class FakeWA:
    def __init__(self):
        self.sent: list[dict] = []
        self._ids = itertools.count(1)
        self.dry_run = False

    def _rec(self, **kw):
        mid = f"wamid.out.{next(self._ids)}"
        kw["id"] = mid
        self.sent.append(kw)
        return SendResult(wa_message_id=mid)

    def send_text(self, to, body, reply_to=None):
        return self._rec(kind="text", to=to, body=body)

    def send_buttons(self, to, body, buttons):
        return self._rec(kind="buttons", to=to, body=body, buttons=buttons)

    def send_template(self, to, name, params, lang=None, button_payloads=None):
        return self._rec(kind="template", to=to, name=name, params=params, payloads=button_payloads)

    def mark_read(self, wa_message_id):
        pass

    def download_media(self, media_id):
        return b"\x89PNG fake", "image/png"

    def to(self, phone):
        return [s for s in self.sent if s["to"] == phone]

    def clear(self):
        self.sent.clear()


@pytest.fixture(scope="session", autouse=True)
def migrate():
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_db():
    eng = get_engine()
    with eng.begin() as c:
        tables = c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version'")).scalars().all()
        c.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
    set_now(SUNDAY_9AM)
    yield
    set_now(None)


@pytest.fixture
def wa():
    fake = FakeWA()
    set_client_override(fake)
    yield fake
    set_client_override(None)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def team(db):
    """فريق تجريبي: مالك، مدير مبيعات، موظفان تحت المدير، وموظفة في قسم آخر."""
    owner = User(name="المالك التجريبي", phone="966500000001", role=Role.owner, username="owner", is_demo=True)
    from app.security import hash_password
    owner.password_hash = hash_password("owner-pass")
    mgr = User(name="سلطان المدير", phone="966500000002", role=Role.manager, is_demo=True, username="sultan",
               password_hash=hash_password("mgr-pass"))
    db.add_all([owner, mgr])
    db.flush()
    fahad = User(name="فهد العتيبي", phone="966500000003", role=Role.employee, manager_id=mgr.id, is_demo=True,
                 username="fahad", password_hash=hash_password("emp-pass"))
    sara = User(name="سارة القحطاني", phone="966500000004", role=Role.employee, manager_id=mgr.id, is_demo=True)
    haifa = User(name="هيفاء الشمري", phone="966500000005", role=Role.employee, is_demo=True, aliases=["هيفا"])
    db.add_all([fahad, sara, haifa])
    db.commit()
    return {"owner": owner, "mgr": mgr, "fahad": fahad, "sara": sara, "haifa": haifa}


_mid = itertools.count(1)


def payload_text(phone, text_, *, pnid="PN_NOOF", context=None, name=None):
    m = {"from": phone, "id": f"wamid.in.{next(_mid)}", "timestamp": "0", "type": "text", "text": {"body": text_}}
    if context:
        m["context"] = {"id": context}
    return _wrap(m, pnid, phone, name)


def payload_button(phone, button_id, title="زر", pnid="PN_NOOF"):
    m = {"from": phone, "id": f"wamid.in.{next(_mid)}", "timestamp": "0", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}}}
    return _wrap(m, pnid, phone)


def payload_image(phone, caption=None, pnid="PN_NOOF"):
    m = {"from": phone, "id": f"wamid.in.{next(_mid)}", "timestamp": "0", "type": "image",
         "image": {"id": "MEDIA1", "mime_type": "image/png", **({"caption": caption} if caption else {})}}
    return _wrap(m, pnid, phone)


def _wrap(m, pnid, phone, name=None):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": pnid, "display_phone_number": "x"},
        "contacts": [{"wa_id": phone, "profile": {"name": name or "عميل"}}], "messages": [m]}}]}]}


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()


@pytest.fixture
def http():
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


@pytest.fixture
def send(http):
    """يرسل Webhook موقّعًا ثم يشغّل العامل الخلفي حتى يفرغ الطابور."""
    def _send(payload):
        body = json.dumps(payload).encode()
        r = http.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sign(body),
                                                                    "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        drain()
    return _send


def drain():
    from app.worker import run_jobs_once
    s = SessionLocal()
    try:
        for _ in range(50):
            if run_jobs_once(s, 50) == 0:
                break
    finally:
        s.close()


def tick():
    from app.worker import scheduler_tick
    s = SessionLocal()
    try:
        scheduler_tick(s)
    finally:
        s.close()
    drain()
