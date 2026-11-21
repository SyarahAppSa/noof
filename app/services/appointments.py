"""المواعيد: إنشاء، مزامنة التقويم، التأكيد بعد موافقة المالك، التذكير بملخص."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import Appointment, ApprovalKind, Client, Message, User
from app.queue import enqueue
from app.services import approvals, clients as client_svc, directory
from app.timeutil import fmt
from app.whatsapp import outbox


def create(db: Session, *, title: str, start_at: datetime, duration_min: int = 60, client: Client | None = None,
           person_name: str | None = None, person_phone: str | None = None, created_by: User | None = None,
           location: str | None = None, request_confirmation: bool = True) -> Appointment:
    appt = Appointment(title=title, start_at=start_at, end_at=start_at + timedelta(minutes=duration_min),
                       client_id=client.id if client else None, person_name=person_name or (client.name if client else None),
                       person_phone=person_phone or (client.phone if client else None), location=location,
                       created_by_id=created_by.id if created_by else None, status="tentative")
    db.add(appt)
    db.flush()
    audit(db, "appointment.created", actor_type="user" if created_by else "system",
          actor_id=created_by.id if created_by else None, entity_type="appointment", entity_id=appt.id)
    enqueue(db, "calendar_sync", {"appointment_id": appt.id})
    if request_confirmation and appt.person_phone:
        approvals.request(
            db, ApprovalKind.appointment_confirmation,
            f"إرسال تأكيد موعد «{title}» إلى {appt.person_name or appt.person_phone} {fmt(start_at)}",
            {"appointment_id": appt.id}, requested_by=created_by, client_id=appt.client_id)
    else:
        appt.status = "confirmed"
    return appt


def conflicts(db: Session, start_at: datetime, end_at: datetime) -> list[Appointment]:
    return db.scalars(select(Appointment).where(
        Appointment.status != "cancelled", Appointment.start_at < end_at, Appointment.end_at > start_at)).all()


def briefing(db: Session, appt: Appointment) -> str:
    lines = [f"🗓️ *تذكير بموعد* {fmt(appt.start_at)}", f"الموضوع: {appt.title}"]
    if appt.person_name:
        lines.append(f"الشخص: {appt.person_name}")
    if appt.client:
        c = appt.client
        lines.append(f"الشركة: {c.company or c.name}" + (f" — {c.sector}" if c.sector else ""))
        last = db.scalar(select(Message).where(Message.client_id == c.id).order_by(Message.created_at.desc()).limit(1))
        if last:
            lines.append(f"آخر محادثة ({fmt(last.created_at)}): {(last.summary or last.body or '')[:200]}")
        tasks = client_svc.client_open_tasks(db, c.id)
        if tasks:
            lines.append("المهام المفتوحة:")
            lines += [f"• {t.code} {t.title} — {t.assignee.name}" for t in tasks[:5]]
    if appt.location:
        lines.append(f"المكان: {appt.location}")
    return "\n".join(lines)


def send_reminder(db: Session, appt: Appointment):
    owner = directory.get_owner(db)
    if owner:
        outbox.send(db, owner.phone, briefing(db, appt), user_id=owner.id)
    from app.timeutil import utcnow
    appt.reminder_sent_at = utcnow()
