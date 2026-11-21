"""REST API منظم (v1) بصلاحيات دقيقة: المالك كل شيء، المدير فريقه، الموظف مهامه."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import api_user, require_owner
from app.audit import audit
from app.db import get_db
from app.models import (
    Appointment,
    Approval,
    ApprovalStatus,
    AuditLog,
    Client,
    Opportunity,
    OppStage,
    Priority,
    Role,
    Task,
    TaskStatus,
    User,
)
from app.nlp.arabic import normalize_phone
from app.services import appointments as appt_svc
from app.services import approvals as appr_svc
from app.services import reports
from app.services import tasks as task_svc
from app.services.settings import DEFAULTS, all_settings, set_setting

router = APIRouter(prefix="/api/v1")


def task_out(t: Task) -> dict:
    return {"id": t.id, "code": t.code, "title": t.title, "description": t.description, "status": t.status.value,
            "priority": t.priority.value, "assignee": {"id": t.assignee_id, "name": t.assignee.name},
            "due_at": t.due_at, "client_id": t.client_id, "requires_proof": t.requires_proof,
            "reminder_interval_hours": float(t.reminder_interval_hours), "next_reminder_at": t.next_reminder_at,
            "reminders_sent": t.reminders_sent, "created_at": t.created_at, "completed_at": t.completed_at}


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    assignee_id: int
    description: str | None = None
    due_at: datetime | None = None
    priority: Priority = Priority.normal
    client_id: int | None = None
    requires_proof: bool | None = None
    requires_review: bool = False
    reminder_interval_hours: float | None = Field(default=None, gt=0.25, le=168)


class TaskAction(BaseModel):
    action: str  # ack | start | done | cancel | approve | reject
    note: str | None = None


@router.get("/me")
def me(user: User = Depends(api_user)):
    return {"id": user.id, "name": user.name, "role": user.role.value}


@router.get("/tasks")
def list_tasks(status: TaskStatus | None = None, user: User = Depends(api_user), db: Session = Depends(get_db)):
    q = task_svc.visible_tasks_query(db, user)
    if status:
        q = q.where(Task.status == status)
    return [task_out(t) for t in db.scalars(q.order_by(Task.id.desc()).limit(500)).all()]


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, user: User = Depends(api_user), db: Session = Depends(get_db)):
    assignee = db.get(User, body.assignee_id)
    if not assignee or not assignee.is_active:
        raise HTTPException(404, "assignee not found")
    try:
        t = task_svc.create_task(db, title=body.title, assignee=assignee, created_by=user, due_at=body.due_at,
                                 priority=body.priority, description=body.description, client_id=body.client_id,
                                 requires_proof=body.requires_proof, requires_review=body.requires_review,
                                 reminder_hours=body.reminder_interval_hours)
    except task_svc.TaskError as e:
        raise HTTPException(403, str(e))
    db.commit()
    return task_out(t)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, user: User = Depends(api_user), db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t or not task_svc.can_view(db, user, t):
        raise HTTPException(404, "not found")
    out = task_out(t)
    out["events"] = [{"type": e.event_type, "from": e.from_status, "to": e.to_status, "note": e.note,
                      "at": e.created_at, "actor_id": e.actor_user_id} for e in t.events]
    return out


@router.post("/tasks/{task_id}/actions")
def task_action(task_id: int, body: TaskAction, user: User = Depends(api_user), db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t or not task_svc.can_view(db, user, t):
        raise HTTPException(404, "not found")
    try:
        if body.action == "ack":
            msg = task_svc.acknowledge(db, t, user)
        elif body.action == "start":
            msg = task_svc.start(db, t, user)
        elif body.action == "done":
            msg = task_svc.submit_done(db, t, user, note=body.note)
        elif body.action == "cancel":
            task_svc.cancel(db, t, user, body.note)
            msg = "cancelled"
        elif body.action in ("approve", "reject") and t.status == TaskStatus.pending_review:
            if not task_svc.can_assign(db, user, t.assignee):
                raise HTTPException(403, "forbidden")
            if body.action == "approve":
                task_svc.complete(db, t, user, body.note)
            else:
                task_svc.reject_review(db, t, user, body.note)
            msg = body.action
        else:
            raise HTTPException(400, "invalid action")
    except task_svc.TaskError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return {"message": msg, "task": task_out(t)}


# ---------- العملاء والفرص ----------

class ClientIn(BaseModel):
    name: str
    company: str | None = None
    sector: str | None = None
    phone: str | None = None
    email: str | None = None
    status: str = "lead"
    is_vip: bool = False
    owner_user_id: int | None = None


def _client_out(c: Client) -> dict:
    return {"id": c.id, "name": c.name, "company": c.company, "sector": c.sector, "phone": c.phone,
            "status": c.status, "is_vip": c.is_vip, "owner_user_id": c.owner_user_id,
            "last_contact_at": c.last_contact_at, "last_followup_at": c.last_followup_at}


def _client_scope(db: Session, user: User):
    q = select(Client)
    if user.role == Role.employee:
        q = q.where(Client.owner_user_id == user.id)
    elif user.role == Role.manager:
        from app.services.directory import team_ids
        q = q.where(Client.owner_user_id.in_(team_ids(db, user)))
    return q


@router.get("/clients")
def list_clients(user: User = Depends(api_user), db: Session = Depends(get_db)):
    return [_client_out(c) for c in db.scalars(_client_scope(db, user).order_by(Client.id.desc()).limit(500)).all()]


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    data = body.model_dump()
    if data["phone"]:
        data["phone"] = normalize_phone(data["phone"])
    c = Client(**data)
    db.add(c)
    db.flush()
    audit(db, "client.created", actor_type="user", actor_id=user.id, entity_type="client", entity_id=c.id)
    db.commit()
    return _client_out(c)


@router.get("/opportunities")
def list_opps(user: User = Depends(api_user), db: Session = Depends(get_db)):
    q = select(Opportunity)
    if user.role != Role.owner:
        from app.services.directory import team_ids
        q = q.where(Opportunity.owner_user_id.in_(team_ids(db, user)))
    return [{"id": o.id, "client": o.client.name, "title": o.title, "stage": o.stage.value,
             "estimated_value": float(o.estimated_value) if o.estimated_value is not None else None,
             "quote_status": o.quote_status, "contract_status": o.contract_status,
             "collection_status": o.collection_status} for o in db.scalars(q.order_by(Opportunity.id.desc())).all()]


class OppPatch(BaseModel):
    stage: OppStage | None = None
    quote_status: str | None = None
    contract_status: str | None = None
    collection_status: str | None = None
    estimated_value: float | None = None


@router.patch("/opportunities/{opp_id}")
def patch_opp(opp_id: int, body: OppPatch, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    o = db.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(o, k, v)
    audit(db, "opportunity.updated", actor_type="user", actor_id=user.id, entity_type="opportunity", entity_id=o.id,
          details=body.model_dump(exclude_none=True, mode="json"))
    db.commit()
    return {"ok": True}


# ---------- المواعيد ----------

class AppointmentIn(BaseModel):
    title: str
    start_at: datetime
    duration_min: int = 60
    client_id: int | None = None
    person_name: str | None = None
    person_phone: str | None = None
    location: str | None = None


@router.get("/appointments")
def list_appointments(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return [{"id": a.id, "title": a.title, "start_at": a.start_at, "end_at": a.end_at, "status": a.status,
             "person_name": a.person_name, "client_id": a.client_id, "external_event_id": a.external_event_id}
            for a in db.scalars(select(Appointment).order_by(Appointment.start_at.desc()).limit(300)).all()]


@router.post("/appointments", status_code=201)
def create_appointment(body: AppointmentIn, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    client = db.get(Client, body.client_id) if body.client_id else None
    a = appt_svc.create(db, title=body.title, start_at=body.start_at, duration_min=body.duration_min, client=client,
                        person_name=body.person_name,
                        person_phone=normalize_phone(body.person_phone) if body.person_phone else None,
                        location=body.location, created_by=user)
    db.commit()
    return {"id": a.id, "status": a.status}


# ---------- الموافقات والتقارير ----------

@router.get("/approvals")
def list_approvals(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return [{"id": a.id, "code": a.code, "kind": a.kind.value, "status": a.status.value, "summary": a.summary,
             "payload": a.payload, "created_at": a.created_at}
            for a in db.scalars(select(Approval).order_by(Approval.id.desc()).limit(300)).all()]


class Decision(BaseModel):
    approve: bool
    note: str | None = None


@router.post("/approvals/{ap_id}/decide")
def decide(ap_id: int, body: Decision, user: User = Depends(api_user), db: Session = Depends(get_db)):
    ap = db.get(Approval, ap_id)
    if not ap:
        raise HTTPException(404, "not found")
    try:
        msg = appr_svc.decide(db, ap, user, body.approve, body.note)
    except appr_svc.ApprovalError as e:
        raise HTTPException(403, str(e))
    db.commit()
    return {"message": msg, "status": ap.status.value}


@router.get("/reports/{kind}")
def report(kind: str, user: User = Depends(api_user), db: Session = Depends(get_db)):
    if kind == "employees":
        return {"text": reports.employees_report(db, user)}
    if kind == "overdue":
        return {"text": reports.overdue_report(db, user)}
    if user.role != Role.owner:
        raise HTTPException(403, "owner only")
    fn = {"morning": reports.morning, "evening": reports.evening, "decisions": reports.decisions_now,
          "clients": reports.clients_followup_report}.get(kind)
    if not fn:
        raise HTTPException(404, "unknown report")
    return {"text": fn(db)}


@router.get("/audit")
def audit_log(limit: int = 200, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))).all()
    return [{"id": r.id, "at": r.created_at, "actor": f"{r.actor_type}:{r.actor_id}", "action": r.action,
             "entity": f"{r.entity_type}:{r.entity_id}", "details": r.details} for r in rows]


@router.get("/settings")
def get_settings_api(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return all_settings(db)


@router.patch("/settings")
def patch_settings(body: dict, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    unknown = set(body) - set(DEFAULTS)
    if unknown:
        raise HTTPException(400, f"unknown settings: {sorted(unknown)}")
    for k, v in body.items():
        set_setting(db, k, v)
    audit(db, "settings.updated", actor_type="user", actor_id=user.id, details={"keys": sorted(body)})
    db.commit()
    return all_settings(db)


_ = ApprovalStatus
