"""التقارير التنفيذية: صباحي، مسائي، ما يحتاج قراري الآن، تقرير الموظفين."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import llm
from app.config import TZ
from app.models import (
    OPEN_TASK_STATUSES,
    Appointment,
    Approval,
    ApprovalStatus,
    Message,
    Role,
    StaffRequest,
    Task,
    TaskStatus,
    User,
)
from app.services import clients as client_svc
from app.services import directory
from app.services.approvals import KIND_AR
from app.services.tasks import PRIORITY_AR, STATUS_AR
from app.timeutil import fmt, riyadh_now, utcnow


def _day_bounds(days_ahead: int = 0):
    start = riyadh_now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    return start, start + timedelta(days=1)


def _scope(db: Session, viewer: User | None):
    if viewer is None or viewer.role == Role.owner:
        return None
    return directory.team_ids(db, viewer)


def overdue_tasks(db: Session, viewer: User | None = None) -> list[Task]:
    q = select(Task).where(Task.status.in_(OPEN_TASK_STATUSES), Task.status != TaskStatus.pending_review,
                           Task.due_at < utcnow())
    ids = _scope(db, viewer)
    if ids is not None:
        q = q.where(Task.assignee_id.in_(ids))
    return db.scalars(q.order_by(Task.due_at)).all()


def pending_approvals(db: Session) -> list[Approval]:
    return db.scalars(select(Approval).where(Approval.status == ApprovalStatus.pending).order_by(Approval.id)).all()


def important_client_messages(db: Session, since) -> list[Message]:
    return db.scalars(select(Message).where(
        Message.direction == "in", Message.created_at >= since, Message.user_id.is_(None),
        (Message.needs_owner.is_(True)) | (Message.category.in_(["complaint", "urgent", "sales_opportunity"])),
    ).order_by(Message.created_at.desc()).limit(10)).all()


def appointments_between(db: Session, start, end) -> list[Appointment]:
    return db.scalars(select(Appointment).where(Appointment.status != "cancelled", Appointment.start_at >= start,
                                                Appointment.start_at < end).order_by(Appointment.start_at)).all()


def open_staff_requests(db: Session) -> list[StaffRequest]:
    return db.scalars(select(StaffRequest).where(StaffRequest.status == "open").order_by(StaffRequest.id)).all()


def employee_stats(db: Session, viewer: User | None = None, since=None) -> list[dict]:
    since = since or _day_bounds()[0]
    ids = _scope(db, viewer)
    q = select(User).where(User.is_active.is_(True), User.role != Role.owner)
    if ids is not None:
        q = q.where(User.id.in_(ids))
    out = []
    for u in db.scalars(q.order_by(User.name)).all():
        open_n = db.scalar(select(func.count()).select_from(Task).where(
            Task.assignee_id == u.id, Task.status.in_(OPEN_TASK_STATUSES))) or 0
        done_n = db.scalar(select(func.count()).select_from(Task).where(
            Task.assignee_id == u.id, Task.status == TaskStatus.completed, Task.completed_at >= since)) or 0
        late_n = db.scalar(select(func.count()).select_from(Task).where(
            Task.assignee_id == u.id, Task.status.in_(OPEN_TASK_STATUSES), Task.due_at < utcnow())) or 0
        unack = db.scalar(select(func.count()).select_from(Task).where(
            Task.assignee_id == u.id, Task.status == TaskStatus.new)) or 0
        out.append({"user": u, "open": open_n, "done": done_n, "late": late_n, "unack": unack})
    return out


def _focus_items(db: Session) -> list[str]:
    items: list[tuple[int, str]] = []
    for ap in pending_approvals(db)[:5]:
        items.append((90, f"اعتماد {ap.code}: {ap.summary}"))
    for t in overdue_tasks(db)[:5]:
        w = 95 if t.priority.value == "urgent" else 80
        items.append((w, f"متابعة المهمة المتأخرة {t.code} «{t.title}» مع {t.assignee.name}"))
    since = utcnow() - timedelta(hours=24)
    for m in important_client_messages(db, since)[:5]:
        w = 92 if m.category == "complaint" or m.sentiment == "angry" else 70
        who = m.phone
        items.append((w, f"رسالة {('شكوى' if m.category == 'complaint' else 'عميل')} من {who}: {(m.summary or m.body or '')[:80]}"))
    s, e = _day_bounds()
    for a in appointments_between(db, utcnow(), e)[:3]:
        items.append((75, f"التحضير لموعد {fmt(a.start_at, with_day=False)}: {a.title}"))
    for c in client_svc.needing_followup(db, 5):
        items.append((85 if c.is_vip else 60, f"متابعة العميل {c.name}" + (" (مهم)" if c.is_vip else "")))
    items.sort(key=lambda x: -x[0])
    return [t for _, t in items[:5]]


def top5(db: Session) -> list[str]:
    base = _focus_items(db)
    if llm.available() and len(base) >= 3:
        refined = llm.top_priorities("\n".join(f"- {b}" for b in base))
        if refined:
            return refined[:5]
    return base


def decisions_now(db: Session) -> str:
    lines = ["🧭 *ما يحتاج قرارك الآن*"]
    aps = pending_approvals(db)
    if aps:
        lines.append(f"\n*موافقات معلقة ({len(aps)}):*")
        lines += [f"• {a.code} — {KIND_AR[a.kind]}: {a.summary}" for a in aps[:8]]
    od = [t for t in overdue_tasks(db) if t.priority.value in ("urgent", "high") or t.reminders_sent >= 2]
    if od:
        lines.append(f"\n*مهام متأخرة تحتاج تدخلك ({len(od)}):*")
        lines += [f"• {t.code} {t.title} — {t.assignee.name} (منذ {fmt(t.due_at)})" for t in od[:6]]
    msgs = [m for m in important_client_messages(db, utcnow() - timedelta(hours=48)) if m.needs_owner and not m.handled]
    if msgs:
        lines.append(f"\n*رسائل عملاء بانتظار توجيهك ({len(msgs)}):*")
        lines += [f"• {m.phone}: {(m.summary or m.body or '')[:100]}" for m in msgs[:6]]
    reqs = open_staff_requests(db)
    if reqs:
        lines.append(f"\n*طلبات موظفين مفتوحة ({len(reqs)}):*")
        lines += [f"• {r.user.name}: {r.text[:80]}" for r in reqs[:5]]
    if len(lines) == 1:
        return "🧭 لا توجد أمور تحتاج قرارك الآن. كل شيء تحت السيطرة ✅"
    lines.append("\nللرد على موافقة: «موافق A-رقم» أو «ارفض A-رقم».")
    return "\n".join(lines)


def overdue_report(db: Session, viewer: User | None = None) -> str:
    od = overdue_tasks(db, viewer)
    if not od:
        return "✅ لا توجد مهام متأخرة حاليًا."
    lines = [f"⏳ *المهام المتأخرة ({len(od)})*"]
    by_user: dict[str, list[Task]] = {}
    for t in od:
        by_user.setdefault(t.assignee.name, []).append(t)
    for name, ts in by_user.items():
        lines.append(f"\n👤 {name} ({len(ts)}):")
        lines += [f"• {t.code} {t.title} — كان {fmt(t.due_at)} — {PRIORITY_AR[t.priority]}، تذكيرات: {t.reminders_sent}"
                  for t in ts]
    return "\n".join(lines)


def employees_report(db: Session, viewer: User | None = None) -> str:
    stats = employee_stats(db, viewer)
    if not stats:
        return "لا يوجد موظفون مسجلون بعد."
    lines = [f"👥 *تقرير الموظفين اليومي* — {riyadh_now().strftime('%d/%m/%Y')}"]
    for s in stats:
        flag = " ⚠️" if s["late"] else ""
        lines.append(f"• {s['user'].name}{flag}: أنجز اليوم {s['done']} | مفتوحة {s['open']} | متأخرة {s['late']}"
                     + (f" | بلا تأكيد استلام {s['unack']}" if s["unack"] else ""))
    return "\n".join(lines)


def clients_followup_report(db: Session) -> str:
    cs = client_svc.needing_followup(db, 15)
    opps = client_svc.open_opportunities(db)
    lines = ["🤝 *العملاء والفرص التي تحتاج متابعة*"]
    if cs:
        lines.append(f"\n*عملاء بلا متابعة ({len(cs)}):*")
        for c in cs:
            last = fmt(c.last_followup_at or c.last_contact_at) if (c.last_followup_at or c.last_contact_at) else "لم تتم"
            lines.append(f"• {c.name}{' ⭐' if c.is_vip else ''} — آخر متابعة: {last}"
                         + (f" — المسؤول: {c.owner.name}" if c.owner else ""))
    if opps:
        lines.append(f"\n*فرص مبيعات مفتوحة ({len(opps)}):*")
        lines += [f"• {o.client.name}: {o.title} — {client_svc.STAGE_AR[o.stage]}" for o in opps[:10]]
    if len(lines) == 1:
        return "🤝 لا يوجد عملاء أو فرص متأخرة في المتابعة ✅"
    return "\n".join(lines)


def morning(db: Session) -> str:
    s, e = _day_bounds()
    now = riyadh_now()
    lines = [f"☀️ *صباح الخير — تقرير نوف الصباحي*\n{now.strftime('%d/%m/%Y')}"]
    today = appointments_between(db, s, e)
    week = appointments_between(db, e, s + timedelta(days=8))
    lines.append(f"\n🗓️ *مواعيد اليوم ({len(today)}):*" if today else "\n🗓️ لا مواعيد اليوم.")
    lines += [f"• {fmt(a.start_at, with_day=False)} — {a.title}" + (f" ({a.person_name})" if a.person_name else "") for a in today]
    if week:
        lines.append(f"\n📅 *الأسبوع القادم ({len(week)}):*")
        lines += [f"• {fmt(a.start_at)} — {a.title}" for a in week[:8]]
    _common_sections(db, lines, since=utcnow() - timedelta(hours=16))
    focus = top5(db)
    if focus:
        lines.append("\n🎯 *أهم 5 أمور للتركيز اليوم:*")
        lines += [f"{i}. {f}" for i, f in enumerate(focus, 1)]
    return "\n".join(lines)


def evening(db: Session) -> str:
    s, e = _day_bounds()
    lines = [f"🌙 *تقرير نوف المسائي* — {riyadh_now().strftime('%d/%m/%Y')}"]
    done = db.scalars(select(Task).where(Task.status == TaskStatus.completed, Task.completed_at >= s)).all()
    lines.append(f"\n✅ *أُنجز اليوم ({len(done)}):*" if done else "\n✅ لم تُغلق مهام اليوم.")
    lines += [f"• {t.code} {t.title} — {t.assignee.name}" for t in done[:10]]
    _common_sections(db, lines, since=s)
    tomorrow = appointments_between(db, e, e + timedelta(days=1))
    if tomorrow:
        lines.append(f"\n🗓️ *مواعيد الغد ({len(tomorrow)}):*")
        lines += [f"• {fmt(a.start_at, with_day=False)} — {a.title}" for a in tomorrow]
    return "\n".join(lines)


def _common_sections(db: Session, lines: list[str], since):
    od = overdue_tasks(db)
    if od:
        lines.append(f"\n⏳ *مهام متأخرة ({len(od)}):*")
        lines += [f"• {t.code} {t.title} — {t.assignee.name}" for t in od[:8]]
    aps = pending_approvals(db)
    if aps:
        lines.append(f"\n🟡 *تحتاج موافقتك ({len(aps)}):*")
        lines += [f"• {a.code}: {a.summary}" for a in aps[:6]]
    msgs = important_client_messages(db, since)
    if msgs:
        lines.append(f"\n💬 *أهم رسائل العملاء ({len(msgs)}):*")
        lines += [f"• {m.phone}: {(m.summary or m.body or '')[:90]}" for m in msgs[:6]]
    cs = client_svc.needing_followup(db, 6)
    if cs:
        lines.append(f"\n🤝 *عملاء يحتاجون متابعة ({len(cs)}):*")
        lines += [f"• {c.name}{' ⭐' if c.is_vip else ''}" for c in cs]
    opps = client_svc.open_opportunities(db, since=since)
    if opps:
        lines.append(f"\n💼 *فرص مبيعات جديدة ({len(opps)}):*")
        lines += [f"• {o.client.name}: {o.title}" for o in opps[:6]]
    reqs = open_staff_requests(db)
    if reqs:
        lines.append(f"\n📨 *طلبات الموظفين المفتوحة ({len(reqs)}):*")
        lines += [f"• {r.user.name}: {r.text[:70]}" for r in reqs[:6]]
    stats = employee_stats(db)
    if stats:
        lines.append("\n👥 *إنجاز الفريق:*")
        lines += [f"• {s['user'].name}: أنجز {s['done']} | مفتوحة {s['open']} | متأخرة {s['late']}" for s in stats]


def task_status_text(t: Task) -> str:
    ev = t.events[-3:] if t.events else []
    hist = "\n".join(f"  - {fmt(e.created_at)}: {e.event_type}" + (f" → {STATUS_AR.get(TaskStatus(e.to_status), e.to_status)}"
                                                                   if e.to_status else "") for e in ev)
    return (f"📌 {t.code}: {t.title}\nالمسؤول: {t.assignee.name}\nالحالة: {STATUS_AR[t.status]}\n"
            f"الموعد: {fmt(t.due_at)}\nالتذكيرات: {t.reminders_sent}\nآخر الأحداث:\n{hist or '  —'}")


_ = TZ
