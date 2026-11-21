from datetime import datetime, timezone

from app.config import TZ

AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]

_now_override: datetime | None = None


def utcnow() -> datetime:
    if _now_override is not None:
        return _now_override
    return datetime.now(timezone.utc)


def set_now(dt: datetime | None):
    """للاختبارات فقط: تثبيت الوقت الحالي."""
    global _now_override
    _now_override = dt.astimezone(timezone.utc) if dt else None


def riyadh_now() -> datetime:
    return utcnow().astimezone(TZ)


def to_riyadh(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def fmt(dt: datetime | None, with_day: bool = True) -> str:
    """تنسيق عربي للتاريخ والوقت بتوقيت الرياض."""
    if dt is None:
        return "—"
    d = to_riyadh(dt)
    hour = d.hour % 12 or 12
    ampm = "ص" if d.hour < 12 else "م"
    t = f"{hour}:{d.minute:02d} {ampm}"
    if not with_day:
        return t
    today = riyadh_now().date()
    delta = (d.date() - today).days
    if delta == 0:
        day = "اليوم"
    elif delta == 1:
        day = "غدًا"
    elif delta == -1:
        day = "أمس"
    else:
        day = f"{AR_WEEKDAYS[d.weekday()]} {d.day}/{d.month}"
    return f"{day} {t}"
