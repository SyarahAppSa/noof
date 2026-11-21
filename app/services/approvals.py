"""الموافقات: لا يُنفذ أي إجراء حساس (رسالة لطرف خارجي جديدة، سعر نهائي، عقد، مال، حذف، التزام) إلا بموافقة المالك."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import Appointment, Approval, ApprovalKind, ApprovalStatus, Role, Task, User
from app.services import directory
from app.timeutil import fmt, utcnow
from app.whatsapp import outbox

KIND_AR = {
    ApprovalKind.send_message: "إرسال رسالة لطرف خارجي",
    ApprovalKind.financial: "إجراء مالي",
    ApprovalKind.final_price: "سعر نهائي",
    ApprovalKind.contract: "عقد",
    ApprovalKind.delete_data: "حذف بيانات",
    ApprovalKind.commitment: "التزام رسمي",
    ApprovalKind.broadcast: "رسالة جماعية",
    ApprovalKind.appointment_confirmation: "تأكيد موعد",
    ApprovalKind.task_review: "مراجعة إنجاز مهمة",
    ApprovalKind.deadline_extension: "تمديد موعد مهمة",
}

OWNER_ONLY = set(ApprovalKind) - {ApprovalKind.task_review, ApprovalKind.deadline_extension}


class ApprovalError(Exception):
    pass


def request(db: Session, kind: ApprovalKind, summary: str, payload: dict, *, requested_by: User | None = None,
            client_id: int | None = None, task_id: int | None = None, source_message_id: int | None = None,
            details: str | None = None) -> Approval:
    ap = Approval(kind=kind, summary=summary, payload=payload, requested_by_id=requested_by.id if requested_by else None,
                  client_id=client_id, task_id=task_id, source_message_id=source_message_id)
    db.add(ap)
    db.flush()
    audit(db, "approval.requested", actor_type="user" if requested_by else "system",
          actor_id=requested_by.id if requested_by else None, entity_type="approval", entity_id=ap.id,
          details={"kind": kind.value})
    owner = directory.get_owner(db)
    if owner:
        body = f"🟡 *طلب موافقة {ap.code}* — {KIND_AR[kind]}\n{summary}"
        if details:
            body += "\n\n" + details.replace("{code}", ap.code)
        body += f"\n\nللاعتماد: «موافق {ap.code}» — للرفض: «ارفض {ap.code}»"
        outbox.send(db, owner.phone, body, user_id=owner.id,
                    buttons=[(f"appr:yes:{ap.id}", "موافق"), (f"appr:no:{ap.id}", "رفض")])
    return ap


def can_decide(db: Session, actor: User, ap: Approval) -> bool:
    if actor.role == Role.owner:
        return True
    if ap.kind in OWNER_ONLY:
        return False
    if actor.role == Role.manager and ap.task_id:
        t = db.get(Task, ap.task_id)
        return bool(t and t.assignee_id in directory.team_ids(db, actor))
    return False


def decide(db: Session, ap: Approval, actor: User, approve: bool, note: str | None = None) -> str:
    if ap.status != ApprovalStatus.pending:
        return f"الطلب {ap.code} تم البت فيه مسبقًا ({'معتمد' if ap.status == ApprovalStatus.approved else 'مرفوض'})."
    if not can_decide(db, actor, ap):
        raise ApprovalError("هذا القرار من صلاحية المالك فقط")
    ap.status = ApprovalStatus.approved if approve else ApprovalStatus.rejected
    ap.decided_by_id = actor.id
    ap.decided_at = utcnow()
    ap.decision_note = note
    audit(db, "approval.decided", actor_type="user", actor_id=actor.id, entity_type="approval", entity_id=ap.id,
          details={"approved": approve, "kind": ap.kind.value})
    if approve:
        result = _execute(db, ap, actor)
        ap.executed_at = utcnow()
        return f"✅ تم اعتماد {ap.code}. {result}"
    _on_reject(db, ap, actor, note)
    return f"❌ تم رفض {ap.code}."


def _execute(db: Session, ap: Approval, actor: User) -> str:
    from app.services import tasks as task_svc

    p = ap.payload or {}
    if ap.kind in (ApprovalKind.send_message, ApprovalKind.commitment, ApprovalKind.final_price):
        if p.get("to") and p.get("text"):
            outbox.send(db, p["to"], p["text"], contact_id=p.get("contact_id"), client_id=ap.client_id,
                        channel=p.get("channel"),
                        template=("client_followup", [p.get("name") or "عميلنا العزيز", p.get("subject") or "طلبكم"])
                        if p.get("allow_template") else None)
            return "تم إرسال الرسالة."
        return "تم التسجيل."
    if ap.kind == ApprovalKind.appointment_confirmation:
        appt = db.get(Appointment, p["appointment_id"])
        if appt:
            appt.status = "confirmed"
            if appt.person_phone:
                outbox.send(db, appt.person_phone,
                            f"السلام عليكم {appt.person_name or ''}،\nنؤكد لكم موعد «{appt.title}» {fmt(appt.start_at)} "
                            "بتوقيت الرياض. نتشرف بلقائكم.",
                            client_id=appt.client_id,
                            template=("appointment_confirmation", [appt.person_name or "عميلنا العزيز", fmt(appt.start_at)]))
                appt.confirmation_sent_at = utcnow()
            return "تم تأكيد الموعد" + (" وإرسال التأكيد للطرف الآخر." if appt.person_phone else ".")
    if ap.kind == ApprovalKind.task_review:
        t = db.get(Task, p["task_id"])
        if t:
            task_svc.complete(db, t, actor, "اعتماد الإنجاز")
            outbox.send(db, t.assignee.phone, f"تم اعتماد إنجاز المهمة {t.code} ✅ شكرًا لك.", user_id=t.assignee_id,
                        task_id=t.id)
            return "أُغلقت المهمة."
    if ap.kind == ApprovalKind.deadline_extension:
        t = db.get(Task, p["task_id"])
        if t and p.get("new_due"):
            t.due_at = datetime.fromisoformat(p["new_due"])
            t.overdue_notified_at = None
            if t.status.value == "overdue":
                t.status = t.status.__class__.in_progress
            outbox.send(db, t.assignee.phone, f"تمت الموافقة على تمديد المهمة {t.code} إلى {fmt(t.due_at)}.",
                        user_id=t.assignee_id, task_id=t.id)
            return "تم تمديد الموعد."
    if ap.requested_by_id:
        req = db.get(User, ap.requested_by_id)
        if req and req.id != actor.id:
            outbox.send(db, req.phone, f"تم اعتماد طلبك {ap.code}: {ap.summary}", user_id=req.id)
    return "تم التسجيل."


def _on_reject(db: Session, ap: Approval, actor: User, note: str | None):
    from app.services import tasks as task_svc

    if ap.kind == ApprovalKind.task_review and ap.task_id:
        t = db.get(Task, ap.task_id)
        if t:
            task_svc.reject_review(db, t, actor, note)
    elif ap.requested_by_id and ap.requested_by_id != actor.id:
        req = db.get(User, ap.requested_by_id)
        if req:
            outbox.send(db, req.phone, f"نعتذر، لم تتم الموافقة على طلبك {ap.code}.", user_id=req.id)


def pending(db: Session) -> list[Approval]:
    return db.scalars(select(Approval).where(Approval.status == ApprovalStatus.pending).order_by(Approval.id)).all()
