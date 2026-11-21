"""سيناريوهات المهام الكاملة عبر واتساب: تكليف، استلام، إنجاز، إثبات، تذكير 12 ساعة، تأخير."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import TZ
from app.models import Message, Priority, Task, TaskStatus
from app.services import tasks as task_svc
from app.timeutil import set_now
from tests.conftest import drain, payload_button, payload_image, payload_text, tick

OWNER = "966500000001"
FAHAD = "966500000003"
MGR = "966500000002"


def _task(db, **kw):
    db.expire_all()
    return db.scalars(select(Task).filter_by(**kw).order_by(Task.id.desc())).first()


def test_owner_assigns_task_by_whatsapp(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز تقرير المبيعات اليوم قبل الساعة 2"))
    t = _task(db)
    assert t is not None
    assert t.assignee_id == team["fahad"].id
    assert t.title == "تجهيز تقرير المبيعات"
    assert t.due_at.astimezone(TZ) == datetime(2026, 11, 22, 14, 0, tzinfo=TZ)
    assert float(t.reminder_interval_hours) == 12
    assert t.status == TaskStatus.new
    # فهد لم يراسل نوف من قبل => خارج نافذة 24 ساعة => قالب معتمد مع أزرار
    to_fahad = wa.to(FAHAD)
    assert to_fahad and to_fahad[0]["kind"] == "template" and to_fahad[0]["name"] == "noof_task_assigned"
    assert f"task:ack:{t.id}" in to_fahad[0]["payloads"]
    # المالك تصله رسالة تأكيد (النافذة مفتوحة لأنه راسل للتو)
    conf = wa.to(OWNER)[-1]
    assert conf["kind"] == "text" and t.code in conf["body"] and "فهد" in conf["body"]
    # السجل
    assert [e.event_type for e in t.events][:2] == ["created", "assignment_sent"]


def test_acknowledge_then_done_closes_task(team, wa, send, db):
    send(payload_text(OWNER, "كلف فهد بمراجعة عقد التوريد بكرة الساعة 11"))
    t = _task(db)
    send(payload_button(FAHAD, f"task:ack:{t.id}", "تم الاستلام"))
    assert _task(db, id=t.id).status == TaskStatus.acknowledged
    assert "تم تسجيل استلام" in wa.to(FAHAD)[-1]["body"]
    wa.clear()
    send(payload_text(FAHAD, "تم"))
    t = _task(db, id=t.id)
    assert t.status == TaskStatus.completed and t.completed_at is not None
    assert t.next_reminder_at is None
    owner_msgs = [m["body"] for m in wa.to(OWNER)]
    assert any("أُنجزت المهمة" in b for b in owner_msgs)
    mgr_msgs = db.scalars(select(Message).where(Message.phone == MGR, Message.direction == "out")).all()
    assert any("أُنجزت المهمة" in m.body for m in mgr_msgs)  # يُرسل للمدير (مؤجل حتى تفتح نافذته)


def test_done_requires_proof_when_requested(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تصوير المستودع اليوم الساعة 4 مع صورة"))
    t = _task(db)
    assert t.requires_proof
    send(payload_text(FAHAD, "تم"))
    assert _task(db, id=t.id).status != TaskStatus.completed
    assert "تتطلب إثبات" in wa.to(FAHAD)[-1]["body"]
    send(payload_image(FAHAD, caption="هذي الصور"))
    t = _task(db, id=t.id)
    assert t.status == TaskStatus.completed
    assert len(t.attachments) == 1


def test_multiple_open_tasks_asks_which_one(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض"))
    send(payload_text(OWNER, "أرسل لفهد تحديث الأسعار"))
    send(payload_text(FAHAD, "تم"))
    assert "أكثر من مهمة" in wa.to(FAHAD)[-1]["body"]
    t = _task(db)
    send(payload_text(FAHAD, f"تم T-{t.id}"))
    assert _task(db, id=t.id).status == TaskStatus.completed


def test_reply_to_assignment_message_resolves_task(team, wa, send, db):
    send(payload_text(FAHAD, "السلام عليكم"))  # يفتح النافذة
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض"))
    send(payload_text(OWNER, "أرسل لفهد تحديث الأسعار"))
    first = db.scalars(select(Task).order_by(Task.id)).first()
    assign_msg = db.scalars(select(Message).where(Message.task_id == first.id, Message.direction == "out",
                                                  Message.phone == FAHAD)).first()
    send(payload_text(FAHAD, "تم", context=assign_msg.wa_message_id))
    assert _task(db, id=first.id).status == TaskStatus.completed


def test_reminder_every_12_hours_respects_work_hours(team, wa, db):
    t = task_svc.create_task(db, title="مهمة بلا موعد", assignee=team["fahad"], created_by=team["owner"])
    db.commit()
    # 9 صباح الأحد + 12 ساعة = 9 مساءً (خارج الدوام) => تؤجل لبداية دوام الاثنين 8:00
    assert t.next_reminder_at.astimezone(TZ) == datetime(2026, 11, 23, 8, 0, tzinfo=TZ)
    set_now(datetime(2026, 11, 22, 22, 0, tzinfo=TZ))
    tick()
    assert _task(db, id=t.id).reminders_sent == 0
    set_now(datetime(2026, 11, 23, 8, 1, tzinfo=TZ))
    wa.clear()
    tick()
    t = _task(db, id=t.id)
    assert t.reminders_sent == 1
    assert wa.to(FAHAD) and wa.to(FAHAD)[0]["name"] == "noof_task_reminder"
    # التذكير التالي بعد 12 ساعة => 8:01 مساءً => يؤجل للثلاثاء 8:00
    assert t.next_reminder_at.astimezone(TZ) == datetime(2026, 11, 24, 8, 0, tzinfo=TZ)
    tick()  # لا تكرار
    assert _task(db, id=t.id).reminders_sent == 1


def test_urgent_task_reminds_faster_and_ignores_work_hours(team, wa, db):
    t = task_svc.create_task(db, title="عاجل", assignee=team["fahad"], created_by=team["owner"],
                             priority=Priority.urgent)
    db.commit()
    assert float(t.reminder_interval_hours) == 3
    assert t.next_reminder_at.astimezone(TZ) == datetime(2026, 11, 22, 12, 0, tzinfo=TZ)


def test_custom_reminder_interval(team, db):
    t = task_svc.create_task(db, title="x", assignee=team["fahad"], created_by=team["owner"], reminder_hours=2)
    db.commit()
    assert t.next_reminder_at.astimezone(TZ) == datetime(2026, 11, 22, 11, 0, tzinfo=TZ)


def test_reminder_before_short_deadline(team, db):
    due = datetime(2026, 11, 22, 14, 0, tzinfo=TZ)
    t = task_svc.create_task(db, title="x", assignee=team["fahad"], created_by=team["owner"], due_at=due)
    db.commit()
    assert t.next_reminder_at.astimezone(TZ) == datetime(2026, 11, 22, 13, 0, tzinfo=TZ)


def test_overdue_alerts_owner_and_direct_manager(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز تقرير المبيعات اليوم قبل الساعة 2"))
    t = _task(db)
    wa.clear()
    set_now(datetime(2026, 11, 22, 14, 5, tzinfo=TZ))
    tick()
    t = _task(db, id=t.id)
    assert t.status == TaskStatus.overdue and t.overdue_notified_at
    owner_alert = [m for m in wa.to(OWNER) if "تنبيه تأخير" in (m.get("body") or "")]
    assert owner_alert, wa.sent
    # المدير المباشر لم يراسل نوف => تنبيهه يُؤجل ويصله قالب "لديك تحديثات"
    mgr_msgs = db.scalars(select(Message).where(Message.phone == MGR, Message.direction == "out")).all()
    assert any("تنبيه تأخير" in m.body for m in mgr_msgs)
    assert any(s["kind"] == "template" and s["name"] == "noof_staff_update" for s in wa.to(MGR))
    tick()  # لا تكرار للتنبيه
    assert len([m for m in wa.to(OWNER) if "تنبيه تأخير" in (m.get("body") or "")]) == 1


def test_deferred_messages_flush_when_person_replies(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض"))
    t = _task(db)
    set_now(datetime(2026, 11, 22, 14, 5, tzinfo=TZ))
    db.query(Task).filter(Task.id == t.id).update({"due_at": datetime(2026, 11, 22, 14, 0, tzinfo=TZ)})
    db.commit()
    tick()
    deferred = db.scalars(select(Message).where(Message.phone == MGR, Message.status == "deferred")).all()
    assert deferred
    wa.clear()
    send(payload_text(MGR, "السلام عليكم"))
    db.expire_all()
    assert all(m.status == "sent" for m in db.scalars(select(Message).where(Message.id.in_([d.id for d in deferred]))))
    assert any("تنبيه تأخير" in (m.get("body") or "") for m in wa.to(MGR))


def test_follow_up_with_employee_every_n_hours(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لهيفاء تجهيز ملف الموردين"))
    t = _task(db)
    send(payload_text(OWNER, "تابع مع هيفاء كل 6 ساعات حتى تنهي الموضوع"))
    t = _task(db, id=t.id)
    assert float(t.reminder_interval_hours) == 6
    assert "كل 6 ساعة" in wa.to(OWNER)[-1]["body"]


def test_follow_up_with_employee_without_tasks_creates_one(team, wa, send, db):
    send(payload_text(OWNER, "تابع مع هيفاء كل 12 ساعة حتى تنهي الموضوع"))
    t = _task(db)
    assert t.assignee_id == team["haifa"].id and float(t.reminder_interval_hours) == 12


def test_employee_extension_request_needs_approval(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض اليوم الساعة 3"))
    t = _task(db)
    send(payload_text(FAHAD, "أحتاج وقت، أقدر أسلمها بكرة الساعة 12"))
    from app.models import Approval
    ap = db.scalars(select(Approval)).first()
    assert ap and ap.kind.value == "deadline_extension"
    send(payload_text(OWNER, f"موافق A-{ap.id}"))
    t = _task(db, id=t.id)
    assert t.due_at.astimezone(TZ) == datetime(2026, 11, 23, 12, 0, tzinfo=TZ)


def test_review_required_flow(team, wa, db, send):
    t = task_svc.create_task(db, title="تصميم الشعار", assignee=team["fahad"], created_by=team["owner"],
                             requires_review=True)
    db.commit()
    send(payload_text(FAHAD, "تم"))
    t = _task(db, id=t.id)
    assert t.status == TaskStatus.pending_review
    from app.models import Approval
    ap = db.scalars(select(Approval)).first()
    send(payload_button(OWNER, f"appr:yes:{ap.id}"))
    assert _task(db, id=t.id).status == TaskStatus.completed


def test_invalid_transition_rejected(team, db):
    t = task_svc.create_task(db, title="x", assignee=team["fahad"], created_by=team["owner"], notify=False)
    task_svc.complete(db, t, team["owner"])
    try:
        task_svc.start(db, t, team["fahad"])
    except task_svc.TaskError:
        pass
    else:
        raise AssertionError("expected TaskError")


def test_cancel_stops_reminders(team, wa, send, db):
    send(payload_text(OWNER, "أرسل لفهد تجهيز العرض"))
    t = _task(db)
    send(payload_text(OWNER, f"الغاء المهمة T-{t.id}"))
    t = _task(db, id=t.id)
    assert t.status == TaskStatus.cancelled and t.next_reminder_at is None
    set_now(datetime(2026, 11, 25, 9, 0, tzinfo=TZ))
    tick()
    assert _task(db, id=t.id).reminders_sent == 0
    _ = timedelta
    drain()
