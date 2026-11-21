"""الصلاحيات والأمان: أدوار المالك/المدير/الموظف/العميل، توقيع Webhook، التشفير، سجل التدقيق."""
import json

import pytest
from sqlalchemy import select, text

from app.models import ApiKey, AuditLog, Message, Task
from app.security import new_api_key
from app.services import tasks as task_svc
from tests.conftest import payload_text, sign

OWNER, MGR, FAHAD, HAIFA = "966500000001", "966500000002", "966500000003", "966500000005"
CLIENT = "966511111111"


def _key(db, user):
    raw, h = new_api_key()
    db.add(ApiKey(user_id=user.id, name="t", key_hash=h))
    db.commit()
    return {"Authorization": f"Bearer {raw}"}


def test_manager_can_assign_only_own_team(team, wa, send, db):
    send(payload_text(MGR, "أرسل لفهد تحديث العروض"))
    assert db.scalars(select(Task)).first().assignee_id == team["fahad"].id
    send(payload_text(MGR, "أرسل لهيفاء تجهيز ملف"))  # هيفاء ليست ضمن فريقه
    assert db.scalars(select(Task)).all().__len__() == 1
    assert "لم أتعرف" in wa.to(MGR)[-1]["body"] or "صلاحية" in wa.to(MGR)[-1]["body"]


def test_employee_cannot_assign_tasks(team, wa, send, db):
    send(payload_text(FAHAD, "أرسل لسارة تجهيز التقرير"))
    assert db.scalars(select(Task)).first() is None
    assert "تم تسجيل طلبك" in wa.to(FAHAD)[-1]["body"]


def test_employee_api_sees_only_own_tasks(team, db, http):
    task_svc.create_task(db, title="لفهد", assignee=team["fahad"], created_by=team["owner"], notify=False)
    task_svc.create_task(db, title="لهيفاء", assignee=team["haifa"], created_by=team["owner"], notify=False)
    db.commit()
    r = http.get("/api/v1/tasks", headers=_key(db, team["fahad"]))
    assert [t["title"] for t in r.json()] == ["لفهد"]
    other = db.scalars(select(Task).where(Task.title == "لهيفاء")).first()
    assert http.get(f"/api/v1/tasks/{other.id}", headers=_key(db, team["fahad"])).status_code == 404


def test_manager_api_sees_team_only(team, db, http):
    task_svc.create_task(db, title="لفهد", assignee=team["fahad"], created_by=team["owner"], notify=False)
    task_svc.create_task(db, title="لسارة", assignee=team["sara"], created_by=team["owner"], notify=False)
    task_svc.create_task(db, title="لهيفاء", assignee=team["haifa"], created_by=team["owner"], notify=False)
    db.commit()
    titles = sorted(t["title"] for t in http.get("/api/v1/tasks", headers=_key(db, team["mgr"])).json())
    assert titles == ["لسارة", "لفهد"]
    assert http.get("/api/v1/approvals", headers=_key(db, team["mgr"])).status_code == 403
    assert http.get("/api/v1/reports/morning", headers=_key(db, team["mgr"])).status_code == 403


def test_employee_cannot_create_task_via_api(team, db, http):
    r = http.post("/api/v1/tasks", json={"title": "x x", "assignee_id": team["sara"].id}, headers=_key(db, team["fahad"]))
    assert r.status_code == 403


def test_api_requires_valid_token(http, team):
    assert http.get("/api/v1/tasks").status_code == 401
    assert http.get("/api/v1/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_client_never_sees_internal_data(team, wa, send, db):
    task_svc.create_task(db, title="سر داخلي", assignee=team["fahad"], created_by=team["owner"], notify=False)
    db.commit()
    for q in ["من لديه مهام متأخرة؟", "ماذا يحتاج قراري اليوم؟", "مهامي", "أرسل تقرير الموظفين"]:
        send(payload_text(CLIENT, q))
    bodies = " ".join(m.get("body") or "" for m in wa.to(CLIENT))
    assert "سر داخلي" not in bodies and "فهد" not in bodies and "T-" not in bodies


def test_only_owner_can_approve_sensitive(team, wa, send, db):
    send(payload_text(CLIENT, "عندي شكوى، الطلب وصل متأخر جدًا"))
    from app.models import Approval
    ap = db.scalars(select(Approval)).first()
    send(payload_text(MGR, f"موافق A-{ap.id}"))
    db.expire_all()
    assert db.get(Approval, ap.id).status.value == "pending"
    assert "المالك فقط" in wa.to(MGR)[-1]["body"]


def test_webhook_rejects_bad_signature(http, team):
    body = json.dumps(payload_text(OWNER, "مساعدة")).encode()
    r = http.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401


def test_webhook_verification_handshake(http):
    ok = http.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                               "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    assert http.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                 "hub.challenge": "1"}).status_code == 403


def test_duplicate_webhook_is_idempotent(http, team, wa, db):
    p = payload_text(OWNER, "أرسل لفهد تجهيز العرض")
    body = json.dumps(p).encode()
    for _ in range(3):
        http.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sign(body)})
    from tests.conftest import drain
    drain()
    assert len(db.scalars(select(Task)).all()) == 1


def test_message_content_encrypted_at_rest(team, send, db):
    send(payload_text(CLIENT, "رقم الحساب السري 12345"))
    raw = db.execute(text("SELECT body FROM messages WHERE direction='in' LIMIT 1")).scalar()
    assert "12345" not in raw and raw.startswith("gAAAA")
    assert db.scalars(select(Message).where(Message.direction == "in")).first().body == "رقم الحساب السري 12345"


def test_audit_log_is_append_only(team, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض"))
    assert db.scalars(select(AuditLog).where(AuditLog.action == "task.created")).first()
    with pytest.raises(Exception):
        db.execute(text("UPDATE audit_log SET action='hacked'"))
        db.commit()
    db.rollback()
    with pytest.raises(Exception):
        db.execute(text("DELETE FROM audit_log"))
        db.commit()
    db.rollback()


def test_dashboard_login_and_rbac(team, http):
    r = http.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]
    page = http.get("/dashboard/login")
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    bad = http.post("/dashboard/login", data={"username": "owner", "password": "x", "csrf": csrf})
    assert "غير صحيحة" in bad.text
    http.post("/dashboard/login", data={"username": "fahad", "password": "emp-pass", "csrf": csrf})
    assert http.get("/dashboard").status_code == 200
    assert http.get("/dashboard/approvals").status_code == 403
    assert http.get("/dashboard/audit").status_code == 403


def test_dashboard_csrf_required(team, http):
    r = http.post("/dashboard/login", data={"username": "owner", "password": "owner-pass", "csrf": "forged"})
    assert r.status_code == 400


def test_owner_dashboard_pages_render(team, http, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض اليوم الساعة 3"))
    send(payload_text(CLIENT, "كم سعر الباقة السنوية؟"))
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', http.get("/dashboard/login").text).group(1)
    http.post("/dashboard/login", data={"username": "owner", "password": "owner-pass", "csrf": csrf})
    t = db.scalars(select(Task)).first()
    for path in ["/dashboard", "/dashboard/tasks", f"/dashboard/tasks/{t.id}", "/dashboard/clients",
                 "/dashboard/appointments", "/dashboard/approvals", "/dashboard/team", "/dashboard/messages",
                 "/dashboard/audit", "/dashboard/settings"]:
        r = http.get(path)
        assert r.status_code == 200, (path, r.text[:500])
        assert 'dir="rtl"' in r.text
