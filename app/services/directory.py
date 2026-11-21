"""التعرف على المرسل والبحث عن الموظفين والعملاء بالاسم."""
import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Client, Contact, Role, User
from app.nlp.arabic import norm_name, normalize, normalize_phone


def owner_phone() -> str:
    return normalize_phone(get_settings().owner_phone)


def get_owner(db: Session) -> User | None:
    return db.scalar(select(User).where(User.role == Role.owner, User.is_active.is_(True)).order_by(User.id).limit(1))


def user_by_phone(db: Session, phone: str) -> User | None:
    return db.scalar(select(User).where(User.phone == phone, User.is_active.is_(True)))


def _name_keys(u) -> list[str]:
    keys = [norm_name(u.name), norm_name(u.name.split()[0])]
    keys += [norm_name(a) for a in (getattr(u, "aliases", None) or [])]
    return [k for k in dict.fromkeys(keys) if k]


def _name_pattern(key: str) -> str:
    return "".join("[هة]" if ch == "ه" else re.escape(ch) for ch in key)


def find_users_in_text(db: Session, text: str, *, scope_ids: set[int] | None = None) -> list[tuple[User, int, int]]:
    """يبحث عن أسماء الموظفين داخل النص الموحّد normalize(text) (يدعم لام الجر: لفهد).
    يرجع (المستخدم, بداية, نهاية) كمواقع في normalize(text)."""
    t = normalize(text)
    found = []
    for u in db.scalars(select(User).where(User.is_active.is_(True))).all():
        if scope_ids is not None and u.id not in scope_ids:
            continue
        for k in sorted(_name_keys(u), key=len, reverse=True):
            m = re.search(r"(?:^|(?<=[\s,]))(?:لل|ل|و|ب)?(" + _name_pattern(k) + r")(?=$|[\s,.:؛?])", t, re.IGNORECASE)
            if m:
                found.append((u, m.start(1), m.end(1)))
                break
    found.sort(key=lambda x: (x[1], -(x[2] - x[1])))
    return found


def find_client(db: Session, name: str) -> Client | None:
    n = norm_name(name)
    if not n:
        return None
    best = None
    for c in db.scalars(select(Client)).all():
        keys = [norm_name(c.name)] + ([norm_name(c.company)] if c.company else [])
        if n in keys:
            return c
        if any(n in k or (len(k) >= 3 and k in n) for k in keys):
            best = best or c
    return best


def team_ids(db: Session, user: User) -> set[int]:
    """المدير يرى نفسه وفريقه المباشر وغير المباشر."""
    ids = {user.id}
    frontier = [user.id]
    while frontier:
        rows = db.scalars(select(User.id).where(User.manager_id.in_(frontier))).all()
        frontier = [r for r in rows if r not in ids]
        ids.update(frontier)
    return ids


def upsert_contact(db: Session, phone: str, profile_name: str | None) -> Contact:
    c = db.scalar(select(Contact).where(Contact.phone == phone))
    if c is None:
        c = Contact(phone=phone, name=profile_name)
        db.add(c)
        db.flush()
        client = db.scalar(select(Client).where(Client.phone == phone))
        if client:
            c.client_id = client.id
            c.kind = "client"
    elif profile_name and not c.name:
        c.name = profile_name
    return c


def search_users(db: Session, q: str):
    like = f"%{q}%"
    return db.scalars(select(User).where(or_(User.name.ilike(like), User.phone.ilike(like)))).all()
