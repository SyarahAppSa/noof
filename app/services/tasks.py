"""إدارة المهام: التكليف، تأكيد الاستلام، التنفيذ، الإثبات، المراجعة، التذكير، التأخير."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import TZ
from app.models import (
    OPEN_TASK_STATUSES,
    Approval,
    ApprovalKind,
    ConversationState,
    Priority,
    Role,
    Task,
    TaskAttachment,
    TaskEvent,
    TaskStatus,
    User,
)
from app.nlp.dates import parse_time_of_day
from app.services import directory
from app.services.settings import get_setting
from app.timeutil import fmt, utcnow
from app.whatsapp import outbox

STATUS_AR = {
    TaskStatus.new: "جديدة",
    TaskStatus.acknowledged: "تم الاستلام",
    TaskStatus.in_progress: "تحت التنفيذ",
    TaskStatus.pending_review: "بانتظار مراجعة",
    TaskStatus.completed: "مكتملة",
    TaskStatus.overdue: "متأخرة",
    TaskStatus.cancelled: "ملغاة",
}
PRIORITY_AR = {Priority.low: "منخفضة", Priority.normal: "عادية", Priority.high: "مرتفعة", Priority.urgent: "عاجلة"}

ALLOWED = {
    TaskStatus.new: {TaskStatus.acknowledged, TaskStatus.in_progress, TaskStatus.pending_review, TaskStatus.completed,
                     TaskStatus.overdue, TaskStatus.cancelled},
    TaskStatus.acknowledged: {TaskStatus.in_progress, TaskStatus.pending_review, TaskStatus.completed,
                              TaskStatus.overdue, TaskStatus.cancelled},
    TaskStatus.in_progress: {TaskStatus.pending_review, TaskStatus.completed, TaskStatus.overdue,
                             TaskStatus.cancelled},
    TaskStatus.overdue: {TaskStatus.acknowledged, TaskStatus.in_progress, TaskStatus.pending_review,
                         TaskStatus.completed, TaskStatus.cancelled},
    TaskStatus.pending_review: {TaskStatus.completed, TaskStatus.in_progress, TaskStatus.overdue,
                                TaskStatus.cancelled},
    TaskStatus.completed: set(),
    TaskStatus.cancelled: set(),
}
REMINDABLE = (TaskStatus.new, TaskStatus.acknowledged, TaskStatus.in_progress, TaskStatus.overdue)


class TaskError(Exception):
    pass


# ---------- التوقيت ----------

def default_interval(db: Session, priority: Priority) -> float:
    if priority == Priority.urgent:
        return float(get_setting(db, "urgent_reminder_hours"))
    if priority == Priority.high:
        return float(get_setting(db, "high_reminder_hours"))
    return float(get_setting(db, "default_reminder_hours"))


def shift_to_work_hours(db: Session, when: datetime) -> datetime:
    """إذا وقع التذكير خارج ساعات/أيام العمل يُؤجل لبداية الدوام التالي."""
    start = parse_time_of_day(get_setting(db, "work_start"))
    end = parse_time_of_day(get_setting(db, "work_end"))
    days = set(get_setting(db, "work_days"))
    local = when.astimezone(TZ)
    for _ in range(14):
        if local.weekday() in days:
            day_start = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            day_end = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            if local < day_start:
                return day_start
            if local <= day_end:
                return local
        local = (local + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if local.weekday() in days:
            return local
    return when


def next_reminder(db: Session, task: Task, base: datetime | None = None) -> datetime:
    base = base or utcnow()
    at = base + timedelta(hours=float(task.reminder_interval_hours))
    # لا نتجاوز الموعد النهائي دون تذكير قبله بساعة للمهام ذات المهلة القصيرة
    if task.due_at and base < task.due_at - timedelta(hours=1) < at:
        at = task.due_at - timedelta(hours=1)
    if get_setting(db, "reminders_respect_work_hours") and task.priority != Priority.urgent:
        at = shift_to_work_hours(db, at)
    return at


# ---------- العرض ----------

def card(task: Task, *, for_assignee: bool = True) -> str:
    lines = [f"📌 *مهمة {task.code}*: {task.title}"]
    if task.description:
        lines.append(f"التفاصيل: {task.description}")
    lines.append(f"الأولوية: {PRIORITY_AR[task.priority]}")
    lines.append(f"الموعد النهائي: {fmt(task.due_at)}")
    if task.related_party or task.client:
        lines.append(f"الجهة: {task.related_party or task.client.name}")
    if not for_assignee:
        lines.append(f"المسؤول: {task.assignee.name}")
        lines.append(f"الحالة: {STATUS_AR[task.status]}")
    if task.requires_proof and for_assignee:
        lines.append("⚠️ عند الإنجاز أرسل «تم» مع ملف أو صورة إثبات.")
    return "\n".join(lines)


def _event(db: Session, task: Task, etype: str, actor: User | None, note: str | None = None,
           frm: TaskStatus | None = None, to: TaskStatus | None = None):
    db.add(TaskEvent(task_id=task.id, actor_user_id=actor.id if actor else None, event_type=etype,
                     from_status=frm.value if frm else None, to_status=to.value if to else None, note=note))


def _set_status(db: Session, task: Task, to: TaskStatus, actor: User | None, note: str | None = None):
    frm = task.status
    if to == frm:
        return
    if to not in ALLOWED[frm]:
        raise TaskError(f"لا يمكن تحويل المهمة من «{STATUS_AR[frm]}» إلى «{STATUS_AR[to]}»")
    task.status = to
    _event(db, task, "status", actor, note, frm, to)
    audit(db, "task.status", actor_type="user" if actor else "system", actor_id=actor.id if actor else None,
          entity_type="task", entity_id=task.id, details={"from": frm.value, "to": to.value})


def _notify_supervisors(db: Session, task: Task, text: str, *, include_manager: bool = True):
    """يرسل للمالك وللمدير المباشر للموظف (بدون تكرار)."""
    targets: dict[str, User | None] = {}
    owner = directory.get_owner(db)
    if owner:
        targets[owner.phone] = owner
    if include_manager and task.assignee.manager_id:
        mgr = db.get(User, task.assignee.manager_id)
        if mgr and mgr.is_active:
            targets[mgr.phone] = mgr
    if task.created_by and task.created_by.id != task.assignee_id:
        targets.setdefault(task.created_by.phone, task.created_by)
    for phone, u in targets.items():
        outbox.send(db, phone, text, user_id=u.id if u else None, task_id=task.id)


# ---------- الإنشاء ----------

def create_task(db: Session, *, title: str, assignee: User, created_by: User | None, due_at: datetime | None = None,
                priority: Priority = Priority.normal, description: str | None = None, client_id: int | None = None,
                related_party: str | None = None, requires_proof: bool | None = None,
                requires_review: bool = False, reminder_hours: float | None = None, notify: bool = True) -> Task:
    if created_by is not None and not can_assign(db, created_by, assignee):
        raise TaskError("ليست لديك صلاحية تكليف هذا الموظف")
    task = Task(
        title=title.strip()[:300], description=description, assignee_id=assignee.id,
        created_by_id=created_by.id if created_by else None, priority=priority, due_at=due_at,
        client_id=client_id, related_party=related_party,
        requires_proof=bool(get_setting(db, "proof_required_by_default")) if requires_proof is None else requires_proof,
        requires_review=requires_review,
        reminder_interval_hours=reminder_hours or default_interval(db, priority), status=TaskStatus.new,
    )
    db.add(task)
    db.flush()
    db.refresh(task)
    task.next_reminder_at = next_reminder(db, task)
    _event(db, task, "created", created_by, f"تكليف {assignee.name}", None, TaskStatus.new)
    audit(db, "task.created", actor_type="user" if created_by else "system",
          actor_id=created_by.id if created_by else None, entity_type="task", entity_id=task.id,
          details={"assignee": assignee.id, "due": task.due_at.isoformat() if task.due_at else None,
                   "priority": priority.value})
    if notify:
        send_assignment(db, task)
    return task


def send_assignment(db: Session, task: Task):
    a = task.assignee
    by = task.created_by.name if task.created_by else "الإدارة"
    body = (f"السلام عليكم {a.name.split()[0]}،\nكلّفك {by} بالمهمة التالية عبر نوف:\n\n{card(task)}\n\n"
            "نأمل تأكيد الاستلام.")
    m = outbox.send(
        db, a.phone, body, user_id=a.id, task_id=task.id,
        buttons=[(f"task:ack:{task.id}", "تم الاستلام"), (f"task:start:{task.id}", "بدأت التنفيذ"),
                 (f"task:done:{task.id}", "تم الإنجاز")],
        template=("task_assigned", [a.name.split()[0], task.title, fmt(task.due_at), task.code]),
        template_buttons=[f"task:ack:{task.id}", f"task:done:{task.id}"],
    )
    _event(db, task, "assignment_sent", None, f"msg#{m.id}")


# ---------- الصلاحيات ----------

def can_assign(db: Session, actor: User, assignee: User) -> bool:
    if actor.role == Role.owner:
        return True
    if actor.role == Role.manager:
        return assignee.id in directory.team_ids(db, actor)
    return False


def can_view(db: Session, actor: User, task: Task) -> bool:
    if actor.role == Role.owner:
        return True
    if actor.role == Role.manager:
        return task.assignee_id in directory.team_ids(db, actor) or task.created_by_id == actor.id
    return task.assignee_id == actor.id


def visible_tasks_query(db: Session, actor: User):
    q = select(Task)
    if actor.role == Role.manager:
        q = q.where(Task.assignee_id.in_(directory.team_ids(db, actor)))
    elif actor.role == Role.employee:
        q = q.where(Task.assignee_id == actor.id)
    return q


# ---------- إجراءات الموظف ----------

def acknowledge(db: Session, task: Task, actor: User) -> str:
    if task.assignee_id != actor.id:
        raise TaskError("هذه المهمة ليست مسندة إليك")
    if task.status in (TaskStatus.new, TaskStatus.overdue) and task.acknowledged_at is None:
        task.acknowledged_at = utcnow()
        if task.status == TaskStatus.new:
            _set_status(db, task, TaskStatus.acknowledged, actor)
        else:
            _event(db, task, "acknowledged", actor)
        return f"شكرًا لك، تم تسجيل استلام المهمة {task.code} ✅\nالموعد النهائي: {fmt(task.due_at)}"
    return f"المهمة {task.code} حالتها: {STATUS_AR[task.status]}"


def start(db: Session, task: Task, actor: User) -> str:
    if task.assignee_id != actor.id:
        raise TaskError("هذه المهمة ليست مسندة إليك")
    task.acknowledged_at = task.acknowledged_at or utcnow()
    task.started_at = task.started_at or utcnow()
    if task.status != TaskStatus.in_progress:
        _set_status(db, task, TaskStatus.in_progress, actor)
    return f"تم تحديث المهمة {task.code} إلى «تحت التنفيذ» 👍 بالتوفيق."


def submit_done(db: Session, task: Task, actor: User, *, note: str | None = None,
                attachments: list[dict] | None = None) -> str:
    """الموظف يرسل «تم». لا تُغلق المهمة إلا بالإثبات عند الحاجة."""
    if task.assignee_id != actor.id:
        raise TaskError("هذه المهمة ليست مسندة إليك")
    if task.status in (TaskStatus.completed, TaskStatus.cancelled):
        return f"المهمة {task.code} {STATUS_AR[task.status]} مسبقًا."
    for a in attachments or []:
        db.add(TaskAttachment(task_id=task.id, message_id=a.get("message_id"), media_id=a.get("media_id"),
                              mime=a.get("mime"), filename=a.get("filename"), caption=a.get("caption")))
    db.flush()
    has_proof = bool(attachments) or db.query(TaskAttachment).filter(TaskAttachment.task_id == task.id).count() > 0
    if task.requires_proof and not has_proof:
        set_state(db, actor.phone, "awaiting_proof", {"task_id": task.id, "note": note})
        _event(db, task, "proof_requested", None)
        return (f"شكرًا لك. المهمة {task.code} تتطلب إثبات إنجاز 📎\n"
                "فضلًا أرسل الملف أو الصورة الآن (يمكنك إضافة تعليق).")
    clear_state(db, actor.phone)
    task.submitted_at = utcnow()
    task.completion_note = note
    if task.requires_review:
        _set_status(db, task, TaskStatus.pending_review, actor, note)
        reviewer_id = task.created_by_id or (directory.get_owner(db).id if directory.get_owner(db) else None)
        ap = Approval(kind=ApprovalKind.task_review, summary=f"مراجعة إنجاز {task.code}: {task.title}",
                      payload={"task_id": task.id}, requested_by_id=actor.id, task_id=task.id)
        db.add(ap)
        db.flush()
        reviewer = db.get(User, reviewer_id) if reviewer_id else None
        if reviewer:
            outbox.send(db, reviewer.phone,
                        f"📥 {actor.name} أنهى المهمة {task.code}: {task.title}\n"
                        f"الملاحظة: {note or '—'}\nالمرفقات: {len(task.attachments)}\nهل تعتمد الإنجاز؟",
                        user_id=reviewer.id, task_id=task.id,
                        buttons=[(f"appr:yes:{ap.id}", "اعتماد"), (f"appr:no:{ap.id}", "إعادة للموظف")])
        return f"تم استلام إنجاز المهمة {task.code} ✅ وهي الآن بانتظار المراجعة."
    complete(db, task, actor, note)
    return f"ممتاز، تم إغلاق المهمة {task.code} كمكتملة ✅ شكرًا لك."


def complete(db: Session, task: Task, actor: User | None, note: str | None = None):
    _set_status(db, task, TaskStatus.completed, actor, note)
    task.completed_at = utcnow()
    task.next_reminder_at = None
    late = task.due_at and task.completed_at > task.due_at
    _notify_supervisors(
        db, task,
        f"✅ أُنجزت المهمة {task.code}: {task.title}\nبواسطة: {task.assignee.name}"
        + (" (بعد الموعد)" if late else "") + (f"\nملاحظة: {note}" if note else ""),
        include_manager=True,
    )


def reject_review(db: Session, task: Task, actor: User, reason: str | None = None):
    _set_status(db, task, TaskStatus.in_progress, actor, reason)
    task.next_reminder_at = next_reminder(db, task)
    outbox.send(db, task.assignee.phone,
                f"🔁 أُعيدت المهمة {task.code} للاستكمال.\nالسبب: {reason or 'يرجى التواصل مع المسؤول'}",
                user_id=task.assignee_id, task_id=task.id)


def cancel(db: Session, task: Task, actor: User, reason: str | None = None):
    if not can_assign(db, actor, task.assignee):
        raise TaskError("ليست لديك صلاحية إلغاء هذه المهمة")
    _set_status(db, task, TaskStatus.cancelled, actor, reason)
    task.next_reminder_at = None
    outbox.send(db, task.assignee.phone, f"تم إلغاء المهمة {task.code}: {task.title}", user_id=task.assignee_id,
                task_id=task.id)


# ---------- التذكير والتأخير (يستدعيها المجدول) ----------

def send_reminder(db: Session, task: Task) -> bool:
    if task.status not in REMINDABLE:
        task.next_reminder_at = None
        return False
    now = utcnow()
    task.reminders_sent += 1
    task.last_reminder_at = now
    overdue = task.due_at and now > task.due_at
    head = "⏰ تذكير" + (" (المهمة متأخرة)" if overdue else "")
    ack = "" if task.acknowledged_at else "\nلم يصلنا تأكيد الاستلام بعد."
    outbox.send(
        db, task.assignee.phone,
        f"{head} رقم {task.reminders_sent}\n{card(task)}\nالحالة: {STATUS_AR[task.status]}{ack}\n"
        "أرسل «تم» عند الإنجاز، أو «جاري» إذا كانت تحت التنفيذ.",
        user_id=task.assignee_id, task_id=task.id,
        buttons=[(f"task:done:{task.id}", "تم الإنجاز"), (f"task:start:{task.id}", "تحت التنفيذ"),
                 (f"task:ack:{task.id}", "تم الاستلام")],
        template=("task_reminder", [task.code, task.title, fmt(task.due_at)]),
        template_buttons=[f"task:done:{task.id}", f"task:start:{task.id}"],
    )
    _event(db, task, "reminder", None, f"تذكير #{task.reminders_sent}")
    task.next_reminder_at = next_reminder(db, task, now)
    return True


def mark_overdue(db: Session, task: Task) -> bool:
    if task.status not in (TaskStatus.new, TaskStatus.acknowledged, TaskStatus.in_progress) or task.overdue_notified_at:
        return False
    prev = task.status
    _set_status(db, task, TaskStatus.overdue, None, "تجاوز الموعد النهائي")
    task.overdue_notified_at = utcnow()
    _notify_supervisors(
        db, task,
        f"🚨 *تنبيه تأخير*\nالمهمة {task.code}: {task.title}\nالمسؤول: {task.assignee.name}\n"
        f"الموعد النهائي كان: {fmt(task.due_at)}\nعدد التذكيرات المرسلة: {task.reminders_sent}\n"
        f"آخر حالة قبل التأخير: {STATUS_AR[prev]}",
    )
    outbox.send(db, task.assignee.phone,
                f"🚨 المهمة {task.code} «{task.title}» تجاوزت الموعد النهائي. نرجو الإنجاز أو إبلاغنا بالعائق فورًا.",
                user_id=task.assignee_id, task_id=task.id,
                buttons=[(f"task:done:{task.id}", "تم الإنجاز"), (f"task:start:{task.id}", "تحت التنفيذ")],
                template=("task_reminder", [task.code, task.title, fmt(task.due_at)]))
    if task.priority != Priority.urgent:
        task.next_reminder_at = next_reminder(db, task)
    return True


def open_tasks_for(db: Session, user: User) -> list[Task]:
    return db.scalars(select(Task).where(Task.assignee_id == user.id, Task.status.in_(OPEN_TASK_STATUSES))
                      .order_by(Task.due_at.nulls_last(), Task.id)).all()


def resolve_task_for_reply(db: Session, user: User, text: str | None, context_wa_id: str | None) -> list[Task]:
    """يحدد المهمة المقصودة: رمز T-12 في النص، أو الرد على رسالة المهمة، أو المهمة الوحيدة المفتوحة."""
    import re

    from app.models import Message

    if text:
        m = re.search(r"[tT]\s*-?\s*(\d+)|(?:مهمه|مهمة)\s*(?:رقم)?\s*(\d+)", text)
        if m:
            t = db.get(Task, int(m.group(1) or m.group(2)))
            return [t] if t and t.assignee_id == user.id else []
    if context_wa_id:
        msg = db.scalar(select(Message).where(Message.wa_message_id == context_wa_id))
        if msg and msg.task_id:
            t = db.get(Task, msg.task_id)
            if t and t.assignee_id == user.id:
                return [t]
    return [t for t in open_tasks_for(db, user) if t.status != TaskStatus.pending_review]


# ---------- حالة الحوار ----------

def set_state(db: Session, phone: str, kind: str, data: dict, hours: int = 24):
    st = db.scalar(select(ConversationState).where(ConversationState.phone == phone))
    exp = utcnow() + timedelta(hours=hours)
    if st:
        st.kind, st.data, st.expires_at = kind, data, exp
    else:
        db.add(ConversationState(phone=phone, kind=kind, data=data, expires_at=exp))


def get_state(db: Session, phone: str) -> ConversationState | None:
    st = db.scalar(select(ConversationState).where(ConversationState.phone == phone))
    if st and st.expires_at > utcnow():
        return st
    return None


def clear_state(db: Session, phone: str):
    st = db.scalar(select(ConversationState).where(ConversationState.phone == phone))
    if st:
        db.delete(st)
