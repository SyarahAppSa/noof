"""لوحة التحكم العربية (RTL) — للاستخدام عند الحاجة فقط، فواتساب هو الواجهة الأساسية."""
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import session_user
from app.audit import audit
from app.config import TZ
from app.db import get_db
from app.models import (
    OPEN_TASK_STATUSES,
    Appointment,
    Approval,
    ApprovalStatus,
    AuditLog,
    AutoReplyRule,
    Client,
    Job,
    Message,
    Opportunity,
    Priority,
    Role,
    Task,
    TaskStatus,
    User,
)
from app.nlp.arabic import normalize_phone
from app.security import hash_password, new_api_key, verify_password
from app.services import approvals as appr_svc
from app.services import clients as client_svc
from app.services import directory, reports
from app.services import tasks as task_svc
from app.services.approvals import KIND_AR
from app.services.settings import DEFAULTS, all_settings, set_setting
from app.timeutil import fmt, utcnow

router = APIRouter(prefix="/dashboard")
templates = Jinja2Templates(directory="app/templates")
CAT_AR = {"client": "عميل", "employee": "موظف", "supplier": "مورد", "urgent": "عاجل", "complaint": "شكوى",
          "appointment": "موعد", "sales_opportunity": "فرصة مبيعات", "follow_up": "متابعة", "personal": "شخصية",
          "command": "أمر", "other": "أخرى", "report": "تقرير", "auto_reply": "رد تلقائي", "ack": "رد استلام"}
templates.env.globals.update(CAT_AR=CAT_AR, fmt=fmt, STATUS_AR=task_svc.STATUS_AR, PRIORITY_AR=task_svc.PRIORITY_AR, KIND_AR=KIND_AR,
                             STAGE_AR=client_svc.STAGE_AR, ROLE_AR={"owner": "المالك", "manager": "مدير",
                                                                    "employee": "موظف"})

_login_attempts: dict[str, list[float]] = defaultdict(list)


class NeedLogin(Exception):
    pass


def current(request: Request, db: Session = Depends(get_db)) -> User:
    u = session_user(request, db)
    if not u:
        raise NeedLogin()
    return u


def owner_only(u: User):
    if u.role != Role.owner:
        raise HTTPException(403, "هذه الصفحة للمالك فقط")


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str):
    if not token or token != request.session.get("csrf"):
        raise HTTPException(400, "انتهت صلاحية النموذج، أعد المحاولة")


def render(request: Request, name: str, user: User | None, **ctx):
    from app.config import get_settings
    ctx.update(request=request, user=user, csrf=csrf_token(request),
               sim_enabled=get_settings().simulator_enabled and get_settings().wa_dry_run)
    return templates.TemplateResponse(request, name, ctx)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None, error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form(""),
          db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    ip = request.client.host if request.client else "?"
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < 900]
    if len(_login_attempts[ip]) >= 8:
        return render(request, "login.html", None, error="محاولات كثيرة، حاول بعد 15 دقيقة")
    u = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
    if not u or not verify_password(password, u.password_hash):
        _login_attempts[ip].append(now)
        audit(db, "dashboard.login_failed", details={"username": username}, ip=ip)
        db.commit()
        return render(request, "login.html", None, error="بيانات الدخول غير صحيحة")
    request.session.clear()
    request.session["uid"] = u.id
    audit(db, "dashboard.login", actor_type="user", actor_id=u.id, ip=ip)
    db.commit()
    return RedirectResponse("/dashboard", 303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/dashboard/login", 303)


@router.get("", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    q = task_svc.visible_tasks_query(db, user)
    open_tasks = db.scalars(q.where(Task.status.in_(OPEN_TASK_STATUSES))).all()
    overdue = reports.overdue_tasks(db, None if user.role == Role.owner else user)
    stats = reports.employee_stats(db, None if user.role == Role.owner else user)
    ctx = dict(open_count=len(open_tasks), overdue=overdue, stats=stats)
    if user.role == Role.owner:
        ctx.update(approvals=appr_svc.pending(db), followups=client_svc.needing_followup(db, 8),
                   opps=client_svc.open_opportunities(db)[:8],
                   appts=reports.appointments_between(db, utcnow(), utcnow() + timedelta(days=30))[:8],
                   top5=reports._focus_items(db),
                   important=reports.important_client_messages(db, utcnow() - timedelta(days=2)))
    return render(request, "home.html", user, **ctx)


@router.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, status: str = "", user: User = Depends(current), db: Session = Depends(get_db)):
    q = task_svc.visible_tasks_query(db, user)
    if status:
        q = q.where(Task.status == TaskStatus(status))
    rows = db.scalars(q.order_by(Task.id.desc()).limit(300)).all()
    scope = None if user.role == Role.owner else directory.team_ids(db, user)
    staff = db.scalars(select(User).where(User.is_active.is_(True))).all()
    staff = [s for s in staff if scope is None or s.id in scope]
    clients = db.scalars(select(Client).order_by(Client.name)).all() if user.role == Role.owner else []
    return render(request, "tasks.html", user, tasks=rows, status=status, staff=staff, clients=clients,
                  statuses=list(TaskStatus), priorities=list(Priority))


@router.post("/tasks")
def create_task(request: Request, title: str = Form(...), assignee_id: int = Form(...), due_at: str = Form(""),
                priority: str = Form("normal"), description: str = Form(""), client_id: str = Form(""),
                requires_proof: str = Form(""), reminder_hours: str = Form(""), csrf: str = Form(""),
                user: User = Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    assignee = db.get(User, assignee_id)
    due = datetime.fromisoformat(due_at).replace(tzinfo=TZ) if due_at else None
    try:
        task_svc.create_task(db, title=title, assignee=assignee, created_by=user, due_at=due,
                             priority=Priority(priority), description=description or None,
                             client_id=int(client_id) if client_id else None, requires_proof=bool(requires_proof),
                             reminder_hours=float(reminder_hours) if reminder_hours else None)
    except task_svc.TaskError as e:
        raise HTTPException(403, str(e))
    db.commit()
    return RedirectResponse("/dashboard/tasks", 303)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(task_id: int, request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t or not task_svc.can_view(db, user, t):
        raise HTTPException(404)
    msgs = db.scalars(select(Message).where(Message.task_id == t.id).order_by(Message.id)).all()
    return render(request, "task_detail.html", user, t=t, msgs=msgs,
                  can_manage=task_svc.can_assign(db, user, t.assignee))


@router.post("/tasks/{task_id}/action")
def task_action(task_id: int, request: Request, action: str = Form(...), note: str = Form(""), csrf: str = Form(""),
                user: User = Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    t = db.get(Task, task_id)
    if not t or not task_svc.can_view(db, user, t):
        raise HTTPException(404)
    try:
        if action == "cancel":
            task_svc.cancel(db, t, user, note or None)
        elif action == "complete" and task_svc.can_assign(db, user, t.assignee):
            task_svc.complete(db, t, user, note or "إغلاق من لوحة التحكم")
        elif action == "remind" and task_svc.can_assign(db, user, t.assignee):
            task_svc.send_reminder(db, t)
        elif action == "reopen" and task_svc.can_assign(db, user, t.assignee):
            task_svc.reject_review(db, t, user, note or None)
    except task_svc.TaskError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return RedirectResponse(f"/dashboard/tasks/{task_id}", 303)


@router.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rows = db.scalars(select(Client).order_by(Client.is_vip.desc(), Client.last_contact_at.desc().nulls_last())).all()
    opps = db.scalars(select(Opportunity).order_by(Opportunity.id.desc()).limit(100)).all()
    staff = db.scalars(select(User).where(User.is_active.is_(True))).all()
    return render(request, "clients.html", user, clients=rows, opps=opps, staff=staff)


@router.post("/clients")
def create_client(request: Request, name: str = Form(...), company: str = Form(""), sector: str = Form(""),
                  phone: str = Form(""), is_vip: str = Form(""), owner_user_id: str = Form(""), csrf: str = Form(""),
                  user: User = Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    owner_only(user)
    c = Client(name=name, company=company or None, sector=sector or None,
               phone=normalize_phone(phone) if phone else None, is_vip=bool(is_vip),
               owner_user_id=int(owner_user_id) if owner_user_id else None, status="active")
    db.add(c)
    db.flush()
    audit(db, "client.created", actor_type="user", actor_id=user.id, entity_type="client", entity_id=c.id)
    db.commit()
    return RedirectResponse("/dashboard/clients", 303)


@router.get("/appointments", response_class=HTMLResponse)
def appts_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rows = db.scalars(select(Appointment).order_by(Appointment.start_at.desc()).limit(200)).all()
    return render(request, "appointments.html", user, appts=rows)


@router.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rows = db.scalars(select(Approval).order_by(Approval.status != ApprovalStatus.pending, Approval.id.desc())
                      .limit(200)).all()
    return render(request, "approvals.html", user, approvals=rows)


@router.post("/approvals/{ap_id}")
def decide(ap_id: int, request: Request, decision: str = Form(...), note: str = Form(""), text: str = Form(""),
           csrf: str = Form(""), user: User = Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    ap = db.get(Approval, ap_id)
    if not ap:
        raise HTTPException(404)
    if text and ap.payload and "text" in ap.payload and user.role == Role.owner:
        ap.payload = {**ap.payload, "text": text}
    try:
        appr_svc.decide(db, ap, user, decision == "approve", note or None)
    except appr_svc.ApprovalError as e:
        raise HTTPException(403, str(e))
    db.commit()
    return RedirectResponse("/dashboard/approvals", 303)


@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rows = db.scalars(select(User).order_by(User.role, User.name)).all()
    return render(request, "team.html", user, team=rows, new_key=request.session.pop("new_key", None))


@router.post("/team")
def add_member(request: Request, name: str = Form(...), phone: str = Form(...), role: str = Form("employee"),
               title: str = Form(""), manager_id: str = Form(""), aliases: str = Form(""), username: str = Form(""),
               password: str = Form(""), csrf: str = Form(""), user: User = Depends(current),
               db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    owner_only(user)
    u = User(name=name, phone=normalize_phone(phone), role=Role(role), title=title or None,
             manager_id=int(manager_id) if manager_id else None,
             aliases=[a.strip() for a in aliases.split(",") if a.strip()], username=username or None,
             password_hash=hash_password(password) if password else None)
    db.add(u)
    db.flush()
    audit(db, "user.created", actor_type="user", actor_id=user.id, entity_type="user", entity_id=u.id,
          details={"role": role})
    db.commit()
    return RedirectResponse("/dashboard/team", 303)


@router.post("/team/{uid}/toggle")
def toggle_member(uid: int, request: Request, csrf: str = Form(""), user: User = Depends(current),
                  db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    owner_only(user)
    u = db.get(User, uid)
    if u and u.id != user.id:
        u.is_active = not u.is_active
        audit(db, "user.toggled", actor_type="user", actor_id=user.id, entity_type="user", entity_id=u.id,
              details={"active": u.is_active})
        db.commit()
    return RedirectResponse("/dashboard/team", 303)


@router.post("/team/{uid}/apikey")
def issue_key(uid: int, request: Request, csrf: str = Form(""), user: User = Depends(current),
              db: Session = Depends(get_db)):
    from app.models import ApiKey

    check_csrf(request, csrf)
    owner_only(user)
    raw, h = new_api_key()
    db.add(ApiKey(user_id=uid, name="dashboard", key_hash=h))
    audit(db, "apikey.issued", actor_type="user", actor_id=user.id, entity_type="user", entity_id=uid)
    db.commit()
    request.session["new_key"] = raw
    return RedirectResponse("/dashboard/team", 303)


@router.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, category: str = "", user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    q = select(Message)
    if category:
        q = q.where(Message.category == category)
    rows = db.scalars(q.order_by(Message.id.desc()).limit(200)).all()
    return render(request, "messages.html", user, msgs=rows, category=category)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, "audit.html", user, rows=rows)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    rules = db.scalars(select(AutoReplyRule).order_by(AutoReplyRule.id)).all()
    jobs = {str(k.value if hasattr(k, "value") else k): v
            for k, v in db.execute(select(Job.status, func.count()).group_by(Job.status)).all()}
    return render(request, "settings.html", user, s=all_settings(db), rules=rules, jobs=jobs)


BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
NUM_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


@router.post("/settings")
async def save_settings(request: Request, user: User = Depends(current), db: Session = Depends(get_db)):
    owner_only(user)
    form = await request.form()
    check_csrf(request, form.get("csrf", ""))
    for k in DEFAULTS:
        if k in BOOL_KEYS:
            set_setting(db, k, form.get(k) == "on")
        elif k == "work_days":
            set_setting(db, k, [int(x) for x in form.getlist("work_days")])
        elif k in form:
            v = form.get(k)
            set_setting(db, k, float(v) if k in NUM_KEYS and "." in str(v) else int(v) if k in NUM_KEYS else v)
    audit(db, "settings.updated", actor_type="user", actor_id=user.id)
    db.commit()
    return RedirectResponse("/dashboard/settings", 303)


@router.post("/rules")
def add_rule(request: Request, name: str = Form(...), keywords: str = Form(...), answer: str = Form(...),
             csrf: str = Form(""), user: User = Depends(current), db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    owner_only(user)
    r = AutoReplyRule(name=name, keywords=[k.strip() for k in keywords.split(",") if k.strip()], answer=answer,
                      approved_by_id=user.id)
    db.add(r)
    db.flush()
    audit(db, "auto_reply.approved", actor_type="user", actor_id=user.id, entity_type="auto_reply_rule", entity_id=r.id)
    db.commit()
    return RedirectResponse("/dashboard/settings", 303)


@router.post("/rules/{rid}/toggle")
def toggle_rule(rid: int, request: Request, csrf: str = Form(""), user: User = Depends(current),
                db: Session = Depends(get_db)):
    check_csrf(request, csrf)
    owner_only(user)
    r = db.get(AutoReplyRule, rid)
    if r:
        r.enabled = not r.enabled
        audit(db, "auto_reply.toggled", actor_type="user", actor_id=user.id, entity_type="auto_reply_rule",
              entity_id=r.id, details={"enabled": r.enabled})
        db.commit()
    return RedirectResponse("/dashboard/settings", 303)


# ---------- محاكي واتساب (للتجربة فقط: يعمل مع WA_DRY_RUN) ----------

SIM_CLIENTS = [("966500000021", "عميل تجريبي: شركة الأفق"), ("966500000022", "عميل تجريبي: مؤسسة الندى"),
               ("966500000031", "عميل تجريبي جديد")]
SIM_EXAMPLES = {
    "owner": ["أرسل لفهد تجهيز تقرير المبيعات بكرة قبل الساعة 2", "كلف سارة بمراجعة فواتير المورد اليوم الساعة 4 عاجل مع إثبات",
              "تابع مع هيفاء كل 6 ساعات حتى تنهي الموضوع", "أضف موعدًا مع العميل خالد يوم الثلاثاء الساعة 11",
              "من لديه مهام متأخرة؟", "ماذا يحتاج قراري اليوم؟", "أرسل تقرير الموظفين اليومي", "التقرير الصباحي",
              "ما هي أهم العملاء والفرص التي لم نتابعها؟", "مساعدة"],
    "employee": ["استلمت", "جاري", "تم", "مهامي", "أحتاج وقت، أقدر أسلمها بكرة الساعة 12"],
    "client": ["السلام عليكم، متى أوقات الدوام؟", "كم سعر عقد الصيانة السنوي؟", "عندي شكوى، الطلب وصلني ناقص",
               "أبغى موعد يوم الأربعاء الساعة 10"],
}


def _sim_guard(user: User):
    from app.config import get_settings

    owner_only(user)
    s = get_settings()
    if not (s.simulator_enabled and s.wa_dry_run):
        raise HTTPException(404)


def _sim_people(db: Session):
    people = [(u.phone, f"{u.name} ({'المالك' if u.role == Role.owner else 'مدير' if u.role == Role.manager else 'موظف'})",
               "owner" if u.role in (Role.owner, Role.manager) else "employee")
              for u in db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.role, User.id)).all()]
    return people + [(p, n, "client") for p, n in SIM_CLIENTS]


@router.get("/simulator", response_class=HTMLResponse)
def simulator(request: Request, phone: str = "", user: User = Depends(current), db: Session = Depends(get_db)):
    _sim_guard(user)
    people = _sim_people(db)
    phone = phone or user.phone
    kind = next((k for p, _, k in people if p == phone), "client")
    thread = db.scalars(select(Message).where(Message.phone == phone).order_by(Message.id.desc()).limit(40)).all()[::-1]
    names = {p: n for p, n, _ in people}
    recent = db.scalars(select(Message).where(Message.direction == "out").order_by(Message.id.desc()).limit(15)).all()
    return render(request, "simulator.html", user, people=people, phone=phone, thread=thread, names=names,
                  recent=recent, examples=SIM_EXAMPLES[kind])


@router.post("/simulator")
def simulator_send(request: Request, phone: str = Form(...), text: str = Form(""), action: str = Form("send"),
                   csrf: str = Form(""), user: User = Depends(current), db: Session = Depends(get_db)):
    import uuid

    from app.api.webhook import store_inbound
    from app.whatsapp.outbox import assistant_channel
    from app.whatsapp.webhook import Inbound
    from app.worker import run_jobs_once, scheduler_tick

    _sim_guard(user)
    check_csrf(request, csrf)
    if action == "tick":
        scheduler_tick(db)
    elif text.strip():
        payload = None
        if text.startswith("btn:"):
            payload, text = text[4:], "زر"
        store_inbound(db, Inbound(wa_message_id=f"wamid.sim.{uuid.uuid4().hex}", phone=phone,
                                  phone_number_id=assistant_channel(),
                                  msg_type="interactive" if payload else "text", text=text.strip(),
                                  interactive_payload=payload, profile_name="محاكي"))
        db.commit()
    for _ in range(30):
        if run_jobs_once(db, 50) == 0:
            break
    return RedirectResponse(f"/dashboard/simulator?phone={phone}", 303)
