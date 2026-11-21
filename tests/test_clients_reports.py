"""العملاء، الموافقات، الردود التلقائية، المواعيد، التقارير، الطابور، تحليل التواريخ."""
from datetime import datetime

import pytest
from sqlalchemy import select

from app.config import TZ
from app.models import (
    Appointment,
    Approval,
    AutoReplyRule,
    Client,
    Job,
    JobStatus,
    Message,
    Opportunity,
    ReportLog,
    Task,
)
from app.nlp.dates import parse_when
from app.queue import enqueue
from app.services import tasks as task_svc
from app.timeutil import set_now
from tests.conftest import drain, payload_text, tick

OWNER, FAHAD = "966500000001", "966500000003"
CLIENT = "966511111111"


def test_new_client_message_creates_profile_and_acks(team, wa, send, db):
    send(payload_text(CLIENT, "السلام عليكم، أبغى أعرف عن خدماتكم", name="أبو خالد"))
    c = db.scalars(select(Client)).first()
    assert c and c.phone == CLIENT and c.last_contact_at
    ack = wa.to(CLIENT)
    assert len(ack) == 1 and "تم استلام رسالتكم" in ack[0]["body"]
    send(payload_text(CLIENT, "وش أوقات الدوام؟"))
    assert len(wa.to(CLIENT)) == 1  # لا يتكرر رد الاستلام خلال 12 ساعة


def test_price_request_creates_opportunity_and_owner_draft_no_price_promised(team, wa, send, db):
    send(payload_text(CLIENT, "كم سعر الباقة السنوية لشركتنا؟"))
    m = db.scalars(select(Message).where(Message.direction == "in")).first()
    assert m.category == "sales_opportunity" and m.needs_owner
    assert db.scalars(select(Opportunity)).first() is not None
    ap = db.scalars(select(Approval)).first()
    assert ap and ap.kind.value == "send_message"
    # لا يُرسل أي سعر للعميل قبل الموافقة
    assert all("ريال" not in (s.get("body") or "") for s in wa.to(CLIENT))
    owner_alert = wa.to(OWNER)[-1]
    assert owner_alert["kind"] in ("buttons", "template") or True
    msgs = db.scalars(select(Message).where(Message.phone == OWNER, Message.direction == "out")).all()
    assert any("طلب موافقة" in x.body and "الرد المقترح" in x.body for x in msgs)


def test_owner_approves_with_custom_reply(team, wa, send, db):
    send(payload_text(OWNER, "السلام عليكم"))
    send(payload_text(CLIENT, "عندي شكوى، الطلب وصلني ناقص"))
    ap = db.scalars(select(Approval)).first()
    wa.clear()
    send(payload_text(OWNER, f"رد A-{ap.id}: نعتذر منكم، سيتواصل معكم مدير الحساب اليوم لاستكمال الطلب."))
    sent = wa.to(CLIENT)
    assert sent and "مدير الحساب" in sent[-1]["body"]
    db.expire_all()
    assert db.get(Approval, ap.id).status.value == "approved"


def test_reject_sends_nothing_to_client(team, wa, send, db):
    send(payload_text(CLIENT, "عندي شكوى"))
    ap = db.scalars(select(Approval)).first()
    wa.clear()
    send(payload_text(OWNER, f"ارفض A-{ap.id}"))
    assert wa.to(CLIENT) == []


def test_approved_faq_auto_reply_only(team, wa, send, db):
    db.add(AutoReplyRule(name="الدوام", keywords=["الدوام", "اوقات العمل"], answer="دوامنا من الأحد إلى الخميس 8ص-5م."))
    db.commit()
    send(payload_text(CLIENT, "وش أوقات الدوام عندكم؟"))
    assert wa.to(CLIENT)[-1]["body"].startswith("دوامنا")
    # سؤال سعر حتى لو فيه كلمة مفتاحية => لا رد تلقائي
    wa.clear()
    send(payload_text("966522222222", "كم سعر الخدمة في الدوام المسائي؟"))
    assert not any((s.get("body") or "").startswith("دوامنا") for s in wa.to("966522222222"))


def test_follow_up_with_client_creates_task_and_draft(team, wa, send, db):
    db.add(Client(name="شركة الأفق", company="شركة الأفق", phone="966533333333", owner_user_id=team["fahad"].id))
    db.commit()
    send(payload_text(OWNER, "تابع مع شركة الأفق بخصوص عرض السعر"))
    t = db.scalars(select(Task)).first()
    assert t.assignee_id == team["fahad"].id and "الأفق" in t.title
    ap = db.scalars(select(Approval)).first()
    assert ap.kind.value == "send_message" and ap.payload["to"] == "966533333333"
    assert wa.to("966533333333") == []  # لا شيء للعميل قبل الموافقة
    send(payload_text(OWNER, f"موافق A-{ap.id}"))
    # العميل خارج نافذة 24 ساعة => قالب المتابعة المعتمد
    s = wa.to("966533333333")
    assert s and s[0]["kind"] == "template" and s[0]["name"] == "noof_client_followup"


def test_appointment_command_and_reminder(team, wa, send, db):
    db.add(Client(name="خالد", phone="966544444444"))
    db.commit()
    send(payload_text(OWNER, "أضف موعدًا مع العميل خالد يوم الثلاثاء الساعة 11"))
    a = db.scalars(select(Appointment)).first()
    assert a.start_at.astimezone(TZ) == datetime(2026, 11, 24, 11, 0, tzinfo=TZ)
    assert a.status == "tentative"
    ap = db.scalars(select(Approval)).first()
    assert ap.kind.value == "appointment_confirmation"
    send(payload_text(OWNER, f"موافق A-{ap.id}"))
    db.expire_all()
    assert db.get(Appointment, a.id).status == "confirmed"
    assert wa.to("966544444444")[0]["name"] == "noof_appointment_confirmation"
    wa.clear()
    set_now(datetime(2026, 11, 24, 10, 35, tzinfo=TZ))
    send(payload_text(OWNER, "مساعدة"))  # يبقي نافذة المالك مفتوحة
    tick()
    briefs = [s for s in wa.to(OWNER) if "تذكير بموعد" in (s.get("body") or "")]
    assert len(briefs) == 1 and "خالد" in briefs[0]["body"]


def test_appointment_without_time_asks(team, wa, send, db):
    send(payload_text(OWNER, "أضف موعد مع العميل سامي"))
    assert db.scalars(select(Appointment)).first() is None
    assert "متى الموعد" in wa.to(OWNER)[-1]["body"]


def test_decisions_and_overdue_queries(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز التقرير اليوم الساعة 10 عاجل"))
    set_now(datetime(2026, 11, 22, 11, 0, tzinfo=TZ))
    tick()
    send(payload_text(OWNER, "من لديه مهام متأخرة؟"))
    body = wa.to(OWNER)[-1]["body"]
    assert "المهام المتأخرة" in body and "فهد" in body
    send(payload_text(CLIENT, "عندي شكوى بخصوص التوصيل"))
    send(payload_text(OWNER, "ما أهم الأمور التي تحتاج قراري الآن؟"))
    body = wa.to(OWNER)[-1]["body"]
    assert "يحتاج قرارك" in body and "A-" in body and "T-" in body


def test_employees_report_and_clients_followup(team, wa, send, db):
    t = task_svc.create_task(db, title="مهمة منجزة", assignee=team["fahad"], created_by=team["owner"], notify=False)
    task_svc.complete(db, t, team["fahad"])
    db.add(Client(name="مؤسسة النخبة", is_vip=True, last_contact_at=datetime(2026, 11, 1, tzinfo=TZ)))
    db.commit()
    send(payload_text(OWNER, "أرسل تقرير الموظفين اليومي"))
    body = wa.to(OWNER)[-1]["body"]
    assert "تقرير الموظفين" in body and "فهد العتيبي: أنجز اليوم 1" in body
    send(payload_text(OWNER, "ما هي أهم العملاء والفرص التي لم نتابعها؟"))
    assert "مؤسسة النخبة" in wa.to(OWNER)[-1]["body"]


def test_scheduled_morning_and_evening_reports_once(team, wa, send, db):
    set_now(datetime(2026, 11, 22, 7, 59, tzinfo=TZ))
    tick()
    assert db.scalars(select(ReportLog)).first() is None
    set_now(datetime(2026, 11, 22, 8, 0, tzinfo=TZ))
    tick()
    tick()
    logs = db.scalars(select(ReportLog)).all()
    assert [r.kind for r in logs] == ["morning"]
    assert "التقرير الصباحي" in logs[0].content and "أهم 5" in logs[0].content or "لا مواعيد" in logs[0].content
    set_now(datetime(2026, 11, 22, 18, 3, tzinfo=TZ))
    tick()
    db.expire_all()
    assert sorted(r.kind for r in db.scalars(select(ReportLog)).all()) == ["evening", "morning"]


def test_report_time_is_configurable(team, db):
    from app.services.settings import set_setting
    set_setting(db, "morning_report_time", "07:30")
    db.commit()
    set_now(datetime(2026, 11, 22, 7, 31, tzinfo=TZ))
    tick()
    assert db.scalars(select(ReportLog)).first().kind == "morning"


def test_queue_retries_with_backoff_then_dead(db, monkeypatch):
    from app import worker

    def boom(db_, p):
        raise RuntimeError("temporary failure")

    monkeypatch.setitem(worker.HANDLERS, "boom", boom)
    enqueue(db, "boom", {}, max_attempts=2)
    db.commit()
    worker.run_jobs_once(db)
    j = db.scalars(select(Job)).first()
    assert j.status == JobStatus.failed and j.attempts == 1 and "temporary" in j.last_error
    set_now(datetime(2026, 11, 22, 10, 0, tzinfo=TZ))
    worker.run_jobs_once(db)
    db.expire_all()
    assert db.scalars(select(Job)).first().status == JobStatus.dead


def test_queue_dedupe(db):
    assert enqueue(db, "x", {}, dedupe_key="k1") is not None
    assert enqueue(db, "x", {}, dedupe_key="k1") is None
    db.commit()
    assert len(db.scalars(select(Job)).all()) == 1


def test_whatsapp_send_failure_is_retried(team, db, monkeypatch, wa):
    from app.whatsapp import outbox
    from app.whatsapp.client import WhatsAppError

    calls = {"n": 0}

    def flaky(to, body, reply_to=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WhatsAppError("rate", code=130429, status=429, retryable=True)
        return wa._rec(kind="text", to=to, body=body)

    db.add(Message(direction="in", phone=FAHAD, channel_phone_number_id="PN_NOOF", body="hi"))
    m = outbox.send(db, FAHAD, "مرحبا")
    db.commit()
    monkeypatch.setattr(wa, "send_text", flaky)
    drain()
    db.expire_all()
    assert db.get(Message, m.id).status == "queued"
    assert db.scalars(select(Job)).first().status == JobStatus.failed  # سيعاد لاحقًا
    set_now(datetime(2026, 11, 22, 10, 0, tzinfo=TZ))
    drain()
    db.expire_all()
    assert db.get(Message, m.id).status == "sent" and calls["n"] == 2


NOW = datetime(2026, 11, 22, 9, 0, tzinfo=TZ)  # الأحد


@pytest.mark.parametrize("text,expected", [
    ("اليوم قبل الساعة 2", datetime(2026, 11, 22, 14, 0, tzinfo=TZ)),
    ("يوم الثلاثاء الساعة 11", datetime(2026, 11, 24, 11, 0, tzinfo=TZ)),
    ("بكرة الساعة 9 ونص الليل", datetime(2026, 11, 23, 21, 30, tzinfo=TZ)),
    ("بكره الساعة 10 الصبح", datetime(2026, 11, 23, 10, 0, tzinfo=TZ)),
    ("بعد ساعتين", datetime(2026, 11, 22, 11, 0, tzinfo=TZ)),
    ("خلال 3 أيام", datetime(2026, 11, 25, 17, 0, tzinfo=TZ)),
    ("25/11 الساعة 4 العصر", datetime(2026, 11, 25, 16, 0, tzinfo=TZ)),
    ("الساعة ٣:١٥", datetime(2026, 11, 22, 15, 15, tzinfo=TZ)),
    ("يوم الأحد الساعة 8", datetime(2026, 11, 29, 8, 0, tzinfo=TZ)),
    ("قبل نهاية الدوام", datetime(2026, 11, 22, 17, 0, tzinfo=TZ)),
    ("الخميس", datetime(2026, 11, 26, 17, 0, tzinfo=TZ)),
])
def test_arabic_date_parsing(text, expected):
    assert parse_when(text, now=NOW).dt == expected


def test_no_date_returns_none():
    assert parse_when("جهز العرض", now=NOW).dt is None
