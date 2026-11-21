"""العملاء والمبيعات: ملفات العملاء، الفرص، المتابعات."""
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import OPEN_TASK_STATUSES, Client, Contact, Opportunity, OppStage, Task, User
from app.queue import enqueue
from app.services.settings import get_setting
from app.timeutil import utcnow

STAGE_AR = {OppStage.new: "جديدة", OppStage.contacted: "تم التواصل", OppStage.quotation: "عرض سعر",
            OppStage.negotiation: "تفاوض", OppStage.contract: "عقد", OppStage.won: "مكسوبة", OppStage.lost: "خاسرة"}


def ensure_client_for_contact(db: Session, contact: Contact, *, name: str | None, company: str | None,
                              sector: str | None) -> Client:
    if contact.client_id:
        c = db.get(Client, contact.client_id)
        if company and not c.company:
            c.company = company
        if sector and not c.sector:
            c.sector = sector
        return c
    c = Client(name=company or name or contact.name or contact.phone, company=company, sector=sector,
               phone=contact.phone, status="lead")
    db.add(c)
    db.flush()
    contact.client_id = c.id
    contact.kind = "client"
    audit(db, "client.created", entity_type="client", entity_id=c.id, details={"source": "whatsapp"})
    enqueue(db, "crm_sync", {"entity": "client", "id": c.id})
    return c


def touch_contact(client: Client, *, outbound: bool = False):
    now = utcnow()
    client.last_contact_at = now
    if outbound:
        client.last_followup_at = now


def create_opportunity(db: Session, client: Client, title: str, *, source_message_id: int | None = None,
                       owner: User | None = None) -> Opportunity:
    existing = db.scalar(select(Opportunity).where(
        Opportunity.client_id == client.id, Opportunity.stage.notin_([OppStage.won, OppStage.lost])))
    if existing:
        return existing
    opp = Opportunity(client_id=client.id, title=title[:300], source_message_id=source_message_id,
                      owner_user_id=(owner.id if owner else client.owner_user_id),
                      next_action_at=utcnow() + timedelta(days=1))
    db.add(opp)
    db.flush()
    audit(db, "opportunity.created", entity_type="opportunity", entity_id=opp.id, details={"client": client.id})
    enqueue(db, "crm_sync", {"entity": "opportunity", "id": opp.id})
    return opp


def needing_followup(db: Session, limit: int = 20) -> list[Client]:
    days = int(get_setting(db, "client_followup_days"))
    cutoff = utcnow() - timedelta(days=days)
    q = (select(Client)
         .where(Client.status != "inactive")
         .where(or_(Client.next_followup_at <= utcnow(),
                    Client.last_followup_at.is_(None) & (Client.last_contact_at.is_not(None)),
                    Client.last_followup_at < cutoff))
         .order_by(Client.is_vip.desc(), Client.next_followup_at.nulls_last(), Client.last_contact_at.desc().nulls_last())
         .limit(limit))
    return db.scalars(q).all()


def open_opportunities(db: Session, since=None) -> list[Opportunity]:
    q = select(Opportunity).where(Opportunity.stage.notin_([OppStage.won, OppStage.lost]))
    if since:
        q = q.where(Opportunity.created_at >= since)
    return db.scalars(q.order_by(Opportunity.created_at.desc())).all()


def client_open_tasks(db: Session, client_id: int) -> list[Task]:
    return db.scalars(select(Task).where(Task.client_id == client_id, Task.status.in_(OPEN_TASK_STATUSES))).all()
