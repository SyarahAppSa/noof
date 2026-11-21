from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(db: Session, action: str, *, actor_type: str = "system", actor_id: int | None = None,
          entity_type: str | None = None, entity_id: int | None = None, details: dict | None = None,
          ip: str | None = None) -> None:
    db.add(AuditLog(actor_type=actor_type, actor_id=actor_id, action=action, entity_type=entity_type,
                    entity_id=entity_id, details=details or {}, ip=ip))
