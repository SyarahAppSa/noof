"""العامل الخلفي: ينفذ مهام الطابور + المجدول (تذكيرات، تأخير، تقارير، مواعيد).

التشغيل: python -m app.worker
يمكن تشغيل أكثر من نسخة بأمان: الطابور يستخدم SKIP LOCKED، والمجدول محمي بقفل PostgreSQL استشاري.
"""
import logging
import signal
import time
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.integrations.calendar import get_calendar
from app.integrations.crm import get_crm
from app.integrations.email import send_email
from app.models import Appointment, Client, Message, Opportunity, ReportLog, Task, TaskStatus
from app.queue import claim, enqueue, mark_done, mark_failed
from app.services import appointments as appt_svc
from app.services import directory, inbound, reports
from app.services import tasks as task_svc
from app.services.settings import get_setting
from app.timeutil import riyadh_now, utcnow
from app.whatsapp import outbox
from app.whatsapp.client import get_client

log = logging.getLogger("noof.worker")
SCHEDULER_LOCK = 774411


# ---------- معالجات المهام ----------

def h_process_inbound(db: Session, p: dict):
    msg = db.get(Message, p["message_id"])
    if msg:
        inbound.process(db, msg)


def h_send_message(db: Session, p: dict):
    msg = db.get(Message, p["message_id"])
    if msg and msg.status in ("queued", None):
        outbox.deliver(db, msg)


def h_send_template_nudge(db: Session, p: dict):
    key, params = p["template"]
    get_client(p.get("channel")).send_template(p["phone"], outbox.TEMPLATES.get(key, key), params,
                                               button_payloads=p.get("payloads"))


def h_task_reminder(db: Session, p: dict):
    t = db.get(Task, p["task_id"])
    if t and t.next_reminder_at and t.next_reminder_at <= utcnow() + timedelta(minutes=1):
        task_svc.send_reminder(db, t)


def h_task_overdue(db: Session, p: dict):
    t = db.get(Task, p["task_id"])
    if t:
        task_svc.mark_overdue(db, t)


def h_report(db: Session, p: dict):
    kind = p["kind"]
    owner = directory.get_owner(db)
    if not owner:
        return
    content = reports.morning(db) if kind == "morning" else reports.evening(db)
    db.add(ReportLog(kind=kind, period_key=p["period"], content=content, sent_to=owner.phone))
    outbox.send(db, owner.phone, content, user_id=owner.id, category="report")
    if get_settings().owner_email:
        try:
            send_email(get_settings().owner_email, f"تقرير نوف {'الصباحي' if kind == 'morning' else 'المسائي'}", content)
        except Exception as e:  # noqa: BLE001 — فشل البريد لا يوقف تقرير واتساب
            log.warning("email failed: %s", e)


def h_appointment_reminder(db: Session, p: dict):
    a = db.get(Appointment, p["appointment_id"])
    if a and not a.reminder_sent_at and a.status != "cancelled":
        appt_svc.send_reminder(db, a)


def h_calendar_sync(db: Session, p: dict):
    a = db.get(Appointment, p["appointment_id"])
    if not a:
        return
    cal = get_calendar()
    if cal.name == "none":
        return
    if a.status == "cancelled" and a.external_event_id:
        cal.delete_event(a.external_event_id)
        return
    desc = appt_svc.briefing(db, a)
    a.external_event_id = cal.upsert_event(event_id=a.external_event_id, title=a.title, start=a.start_at, end=a.end_at,
                                           description=desc, location=a.location)
    a.calendar_provider = cal.name


def h_crm_sync(db: Session, p: dict):
    crm = get_crm()
    if crm.name == "internal":
        return
    if p["entity"] == "client":
        c = db.get(Client, p["id"])
        if c:
            c.crm_external_id = crm.upsert_client({"id": c.id, "name": c.name, "company": c.company, "phone": c.phone,
                                                   "sector": c.sector, "status": c.status,
                                                   "crm_external_id": c.crm_external_id}) or c.crm_external_id
    elif p["entity"] == "opportunity":
        o = db.get(Opportunity, p["id"])
        if o:
            o.crm_external_id = crm.upsert_opportunity({"id": o.id, "title": o.title, "stage": o.stage.value,
                                                        "estimated_value": o.estimated_value,
                                                        "client_external_id": o.client.crm_external_id,
                                                        "crm_external_id": o.crm_external_id}) or o.crm_external_id


HANDLERS = {
    "process_inbound": h_process_inbound,
    "send_message": h_send_message,
    "send_template_nudge": h_send_template_nudge,
    "task_reminder": h_task_reminder,
    "task_overdue": h_task_overdue,
    "report": h_report,
    "appointment_reminder": h_appointment_reminder,
    "calendar_sync": h_calendar_sync,
    "crm_sync": h_crm_sync,
}


def run_jobs_once(db: Session, limit: int = 20) -> int:
    jobs = claim(db, limit)
    for job in jobs:
        handler = HANDLERS.get(job.kind)
        try:
            if handler is None:
                raise RuntimeError(f"unknown job kind {job.kind}")
            with db.begin_nested():
                handler(db, job.payload)
            mark_done(db, job)
        except Exception as e:  # noqa: BLE001 — أي خطأ يُسجل ويعاد المحاولة لاحقًا
            log.exception("job %s (%s) failed", job.id, job.kind)
            mark_failed(db, job, e)
        db.commit()
    return len(jobs)


# ---------- المجدول ----------

def scheduler_tick(db: Session) -> None:
    got = db.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": SCHEDULER_LOCK})
    if not got:
        return
    now = utcnow()
    # 1) التذكيرات
    for t in db.scalars(select(Task).where(Task.status.in_(task_svc.REMINDABLE), Task.next_reminder_at <= now)).all():
        enqueue(db, "task_reminder", {"task_id": t.id}, dedupe_key=f"rem:{t.id}:{t.reminders_sent + 1}")
    # 2) التأخير
    for t in db.scalars(select(Task).where(
            Task.status.in_([TaskStatus.new, TaskStatus.acknowledged, TaskStatus.in_progress]),
            Task.due_at < now, Task.overdue_notified_at.is_(None))).all():
        enqueue(db, "task_overdue", {"task_id": t.id}, dedupe_key=f"overdue:{t.id}:{int(t.due_at.timestamp())}")
    # 3) التقارير اليومية (بتوقيت الرياض)
    local = riyadh_now()
    for kind, key in (("morning", "morning_report_time"), ("evening", "evening_report_time")):
        hh, mm = map(int, str(get_setting(db, key)).split(":"))
        target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= local < target + timedelta(hours=2):
            period = local.strftime("%Y-%m-%d")
            enqueue(db, "report", {"kind": kind, "period": period}, dedupe_key=f"report:{kind}:{period}")
    # 4) تذكير المواعيد
    mins = int(get_setting(db, "appointment_reminder_minutes"))
    for a in db.scalars(select(Appointment).where(
            Appointment.status != "cancelled", Appointment.reminder_sent_at.is_(None),
            Appointment.start_at <= now + timedelta(minutes=mins), Appointment.start_at > now)).all():
        enqueue(db, "appointment_reminder", {"appointment_id": a.id}, dedupe_key=f"appt:{a.id}")
    db.commit()


_running = True


def _stop(*_):
    global _running
    _running = False


def run_loop(should_run=lambda: _running):
    s = get_settings()
    last_tick = 0.0
    log.info("Noof worker started (dry_run=%s)", s.wa_dry_run)
    while should_run():
        db = SessionLocal()
        try:
            if time.time() - last_tick >= s.scheduler_tick_seconds:
                scheduler_tick(db)
                last_tick = time.time()
            n = run_jobs_once(db)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            db.rollback()
            n = 0
        finally:
            db.close()
        if n == 0:
            time.sleep(s.worker_poll_seconds)
    log.info("Noof worker stopped")


def start_embedded():
    """للخطط الصغيرة/التجربة: تشغيل العامل داخل عملية الويب في خيط خلفي."""
    import threading

    t = threading.Thread(target=run_loop, name="noof-embedded-worker", daemon=True)
    t.start()
    return t


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_loop()


if __name__ == "__main__":
    main()
