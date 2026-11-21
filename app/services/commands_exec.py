"""تنفيذ أوامر المالك والمدراء القادمة من واتساب."""
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.ai import llm
from app.audit import audit
from app.config import TZ
from app.models import Approval, ApprovalKind, Client, Priority, Role, Task, TaskStatus, User
from app.nlp import commands as cmd
from app.nlp.arabic import normalize
from app.nlp.dates import parse_when
from app.services import appointments as appt_svc
from app.services import approvals as appr_svc
from app.services import directory, reports
from app.services import tasks as task_svc
from app.services.settings import get_setting
from app.timeutil import fmt, riyadh_now, utcnow
from app.whatsapp import outbox

LEAD_JUNK = re.compile(r"^(?:[,:\-–]|\s|ان\s|انه\s|انها\s|بان\s|ب(?=ت)|يقوم ب|تقوم ب|يجهز لي|يسوي|يعمل)+")
TAIL_JUNK = re.compile(r"(?:\s|,|\.|-)+(?:قبل|يوم|في|علي|الساعه|حتي|ضروري|عاجل|مستعجل)?\s*$")


def _scope(db: Session, actor: User) -> set[int] | None:
    return None if actor.role == Role.owner else directory.team_ids(db, actor)


def _remove_spans(t: str, spans: list[tuple[int, int]]) -> str:
    chars = list(t)
    for s, e in spans:
        for i in range(s, e):
            chars[i] = "\0"
    return "".join(chars)


def _extract_title(t: str, name_end: int, spans: list[tuple[int, int]]) -> str:
    masked = _remove_spans(t, spans)
    rest = masked[name_end:]
    rest = rest.replace("\0", " ")
    rest = re.sub(cmd.URGENT_WORDS + "|" + cmd.PROOF_WORDS + r"|اولويه عاليه|اولوية عالية", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip()
    for _ in range(3):
        rest = LEAD_JUNK.sub("", rest).strip()
        rest = TAIL_JUNK.sub("", rest).strip()
    return rest


def _find_client_mention(db: Session, t: str) -> Client | None:
    m = re.search(r"(?:شركه|شركة|العميل|مؤسسه|مؤسسة|لعميل|لشركه|لشركة)\s+([^\s,]+(?:\s+[^\s,]+)?)", t)
    if not m:
        return None
    cand = m.group(1)
    return directory.find_client(db, cand) or directory.find_client(db, cand.split()[0])


def handle(db: Session, actor: User, text: str, *, source_message_id: int | None = None) -> str:
    intent = cmd.parse(text)
    if intent.name == "unknown" or (intent.name == "assign_task" and not directory.find_users_in_text(
            db, text, scope_ids=_scope(db, actor))):
        llm_intent = _llm_intent(db, actor, text)
        if llm_intent:
            intent = llm_intent
    try:
        return _dispatch(db, actor, intent, text, source_message_id)
    except (task_svc.TaskError, appr_svc.ApprovalError) as e:
        return f"⚠️ {e}"


def _llm_intent(db: Session, actor: User, text: str) -> cmd.Intent | None:
    staff = [u.name for u in db.query(User).filter(User.is_active.is_(True)).all()]
    r = llm.parse_command(text, staff=staff, now_iso=riyadh_now().isoformat())
    if not r or r["intent"] == "unknown":
        return None
    name = {"follow_up_employee": "follow_up", "follow_up_client": "follow_up"}.get(r["intent"], r["intent"])
    slots = {"raw": text, "llm": r, "priority": Priority(r["priority"]), "requires_proof": r["requires_proof"]}
    if r.get("report_kind"):
        slots["kind"] = r["report_kind"]
    if r.get("ref_code"):
        slots["code"] = re.sub(r"\D", "", r["ref_code"])
        slots["task_id"] = int(slots["code"]) if slots["code"] else None
    slots["target"] = r.get("employee_name") or r.get("client_name")
    slots["every_hours"] = r.get("reminder_hours")
    slots["subject"] = r.get("subject") or r.get("title")
    slots["person"] = r.get("client_name") or r.get("employee_name")
    return cmd.Intent(name, slots, confident=True)


def _dispatch(db: Session, actor: User, intent: cmd.Intent, text: str, source_message_id: int | None) -> str:
    n, s = intent.name, intent.slots
    if n == "help":
        return cmd.HELP_TEXT
    if n == "assign_task":
        return _assign(db, actor, text, s)
    if n == "follow_up":
        return _follow_up(db, actor, s)
    if n == "list_overdue":
        return reports.overdue_report(db, actor)
    if n == "decisions_needed":
        if actor.role != Role.owner:
            return reports.overdue_report(db, actor)
        return reports.decisions_now(db)
    if n == "send_report":
        kind = s.get("kind", "summary")
        if kind == "employees":
            return reports.employees_report(db, actor)
        if actor.role != Role.owner:
            return reports.employees_report(db, actor) + "\n\n" + reports.overdue_report(db, actor)
        if kind == "morning":
            return reports.morning(db)
        if kind == "clients":
            return reports.clients_followup_report(db)
        if kind == "decisions":
            return reports.decisions_now(db)
        return reports.evening(db)
    if n == "clients_followup":
        return reports.clients_followup_report(db)
    if n == "create_appointment":
        return _appointment(db, actor, s)
    if n in ("approve", "reject"):
        ap = db.get(Approval, int(s["code"])) if s.get("code") else None
        if not ap:
            return "لم أجد طلب الموافقة. تأكد من الرمز مثل A-5."
        if s.get("override_text") and ap.kind == ApprovalKind.send_message and ap.status.value == "pending":
            if actor.role != Role.owner:
                return "هذا القرار من صلاحية المالك فقط"
            ap.payload = {**(ap.payload or {}), "text": s["override_text"]}
            audit(db, "approval.text_overridden", actor_type="user", actor_id=actor.id, entity_type="approval",
                  entity_id=ap.id)
        return appr_svc.decide(db, ap, actor, n == "approve", s.get("note"))
    if n == "task_status":
        t = db.get(Task, s["task_id"]) if s.get("task_id") else None
        if not t or not task_svc.can_view(db, actor, t):
            return "لم أجد المهمة أو ليست ضمن صلاحياتك."
        return reports.task_status_text(t)
    if n == "cancel_task":
        t = db.get(Task, s["task_id"]) if s.get("task_id") else None
        if not t:
            return "لم أجد المهمة."
        task_svc.cancel(db, t, actor, "إلغاء بأمر " + actor.name)
        return f"تم إلغاء المهمة {t.code} وإبلاغ {t.assignee.name}."
    if n == "list_tasks":
        q = task_svc.visible_tasks_query(db, actor).where(Task.status.in_(
            [TaskStatus.new, TaskStatus.acknowledged, TaskStatus.in_progress, TaskStatus.overdue, TaskStatus.pending_review]))
        ts = db.scalars(q.order_by(Task.due_at.nulls_last())).all()
        if not ts:
            return "لا توجد مهام مفتوحة."
        return "📋 *المهام المفتوحة:*\n" + "\n".join(
            f"• {t.code} {t.title} — {t.assignee.name} — {task_svc.STATUS_AR[t.status]} — {fmt(t.due_at)}" for t in ts[:25])
    return ("لم أفهم الطلب بشكل كافٍ 🙏 هل يمكن توضيحه؟\nاكتب «مساعدة» لعرض أمثلة الأوامر.")


def _assign(db: Session, actor: User, text: str, s: dict) -> str:
    t = normalize(text)
    found = directory.find_users_in_text(db, text, scope_ids=_scope(db, actor))
    found = [f for f in found if f[0].id != actor.id] or found
    llm_r = s.get("llm")
    if not found and llm_r and llm_r.get("employee_name"):
        found = directory.find_users_in_text(db, llm_r["employee_name"], scope_ids=_scope(db, actor))
        found = [(u, 0, 0) for u, _, _ in found]
    if not found:
        names = "، ".join(u.name for u in db.query(User).filter(User.is_active.is_(True), User.role != Role.owner).all())
        return f"لم أتعرف على الموظف المقصود. الموظفون المسجلون: {names or 'لا يوجد'}"
    assignee, _, name_end = found[0]
    when = parse_when(t, work_end=get_setting(db, "work_end"))
    due = when.dt
    if llm_r and llm_r.get("due_iso") and not due:
        try:
            due = datetime.fromisoformat(llm_r["due_iso"]).astimezone(TZ)
        except ValueError:
            due = None
    title = (llm_r.get("title") if llm_r and llm_r.get("title") else None) or _extract_title(t, name_end, when.spans)
    if not title:
        title = "مهمة مكلّفة من " + actor.name
    client = _find_client_mention(db, t)
    priority = s.get("priority", Priority.normal)
    task = task_svc.create_task(db, title=title, assignee=assignee, created_by=actor, due_at=due, priority=priority,
                                description=text, client_id=client.id if client else None,
                                requires_proof=s.get("requires_proof") or None)
    warn = ""
    if due and due < utcnow():
        warn = "\n⚠️ الموعد المحدد في الماضي، راجعه."
    return (f"تم ✅ أنشأت المهمة {task.code} وأرسلتها لـ{assignee.name}.\n"
            f"المهمة: {task.title}\nالموعد النهائي: {fmt(task.due_at)}\nالأولوية: {task_svc.PRIORITY_AR[task.priority]}\n"
            f"التذكير: كل {float(task.reminder_interval_hours):g} ساعة حتى الإنجاز"
            + (f"\nمرتبطة بالعميل: {client.name}" if client else "")
            + ("\nيتطلب إثبات إنجاز 📎" if task.requires_proof else "") + warn)


def _follow_up(db: Session, actor: User, s: dict) -> str:
    target = (s.get("target") or "").strip()
    hours = s.get("every_hours")
    subject = s.get("subject")
    users = directory.find_users_in_text(db, target, scope_ids=_scope(db, actor)) if target else []
    if users:
        u = users[0][0]
        open_tasks = task_svc.open_tasks_for(db, u)
        if subject:
            words = [w for w in normalize(subject).split() if len(w) > 2]
            matching = [t for t in open_tasks if any(w in normalize(t.title) for w in words)]
            open_tasks = matching or open_tasks
        interval = float(hours) if hours else float(get_setting(db, "default_reminder_hours"))
        if open_tasks:
            task = sorted(open_tasks, key=lambda x: x.created_at, reverse=True)[0]
            task.reminder_interval_hours = interval
            task.next_reminder_at = utcnow() + timedelta(hours=interval)
            task_svc._event(db, task, "followup_set", actor, f"متابعة كل {interval:g} ساعة")
            outbox.send(db, u.phone,
                        f"السلام عليكم {u.name.split()[0]}، {actor.name} يتابع معك المهمة {task.code} «{task.title}».\n"
                        f"سأذكّرك كل {interval:g} ساعة حتى تنهيها. أرسل «تم» عند الإنجاز 🌷",
                        user_id=u.id, task_id=task.id)
            return (f"تم ✅ سأتابع مع {u.name} كل {interval:g} ساعة بخصوص {task.code} «{task.title}» حتى تُنجز، "
                    "وأبلغك فور الإنجاز أو التأخير.")
        task = task_svc.create_task(db, title=f"متابعة: {subject or 'الموضوع المطلوب'}", assignee=u, created_by=actor,
                                    reminder_hours=interval, description=s.get("raw"))
        return f"لم أجد مهمة مفتوحة لـ{u.name}، فأنشأت المهمة {task.code} وسأتابعها كل {interval:g} ساعة حتى تُنجز."

    # متابعة عميل/شركة
    client = directory.find_client(db, target) if target else None
    if client is None and target:
        client = Client(name=target, status="lead")
        db.add(client)
        db.flush()
        audit(db, "client.created", actor_type="user", actor_id=actor.id, entity_type="client", entity_id=client.id)
    if client is None:
        return "مع من أتابع؟ اذكر اسم الموظف أو العميل."
    client.next_followup_at = utcnow()
    subj = subject or "آخر المستجدات"
    parts = [f"تم ✅ سجّلت متابعة مع {client.name} بخصوص {subj}."]
    responsible = client.owner if client.owner_user_id else None
    if responsible and responsible.is_active:
        t = task_svc.create_task(db, title=f"متابعة مع {client.name} بخصوص {subj}", assignee=responsible,
                                 created_by=actor, client_id=client.id, due_at=utcnow() + timedelta(days=1),
                                 reminder_hours=hours)
        parts.append(f"كلّفت {responsible.name} بالمهمة {t.code} وسأذكّره حتى تُنجز.")
    if client.phone:
        draft = (f"السلام عليكم ورحمة الله،\nنتابع معكم بخصوص {subj}، ونسعد بإفادتنا بأي مستجدات أو استفسارات. "
                 f"مع خالص التحية — {get_setting(db, 'company_name')}")
        ap = appr_svc.request(db, ApprovalKind.send_message, f"رسالة متابعة إلى {client.name} بخصوص {subj}",
                              {"to": client.phone, "text": draft, "name": client.name, "subject": subj,
                               "allow_template": True}, requested_by=actor, client_id=client.id,
                              details=f"المسودة:\n{draft}")
        parts.append(f"وجهّزت مسودة رسالة للعميل بانتظار موافقتك ({ap.code}).")
    else:
        parts.append("لا يوجد رقم مسجل للعميل، سيظهر في تقارير المتابعة.")
    return "\n".join(parts)


def _appointment(db: Session, actor: User, s: dict) -> str:
    raw = s.get("raw") or ""
    when = parse_when(raw, default_hour_for_date=None)
    llm_r = s.get("llm")
    start = when.dt if when.has_time else None
    if not start and llm_r and llm_r.get("due_iso"):
        try:
            start = datetime.fromisoformat(llm_r["due_iso"]).astimezone(TZ)
        except ValueError:
            start = None
    if not start:
        return "متى الموعد بالضبط؟ فضلًا أرسل الأمر مع اليوم والساعة، مثل: أضف موعدًا مع خالد يوم الثلاثاء الساعة 11"
    person = s.get("person")
    client = directory.find_client(db, person) if person else None
    subject = s.get("subject") or (f"اجتماع مع {client.name if client else person}" if (client or person) else "اجتماع")
    clash = appt_svc.conflicts(db, start, start + timedelta(hours=1))
    appt = appt_svc.create(db, title=subject, start_at=start, client=client, person_name=person if not client else None,
                           created_by=actor)
    msg = f"تم ✅ أضفت الموعد: {appt.title}\nالوقت: {fmt(appt.start_at)} (بتوقيت الرياض)"
    if client:
        msg += f"\nالعميل: {client.name}"
    if clash:
        msg += "\n⚠️ يوجد تعارض مع: " + "، ".join(f"{a.title} {fmt(a.start_at, with_day=False)}" for a in clash)
    if appt.person_phone:
        msg += "\nأرسلت لك طلب موافقة لإرسال التأكيد للطرف الآخر."
    msg += f"\nسأذكّرك قبل الموعد بـ{get_setting(db, 'appointment_reminder_minutes')} دقيقة مع ملخص كامل."
    return msg
