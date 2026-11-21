"""الإعدادات التشغيلية القابلة للتعديل (تُحفظ في قاعدة البيانات)."""
from sqlalchemy.orm import Session

from app.models import Setting

DEFAULTS: dict = {
    "morning_report_time": "08:00",
    "evening_report_time": "18:00",
    "work_start": "08:00",
    "work_end": "17:00",
    "work_days": [6, 0, 1, 2, 3],  # الأحد..الخميس (Python weekday: الاثنين=0 ... الأحد=6)
    "default_reminder_hours": 12,
    "urgent_reminder_hours": 3,
    "high_reminder_hours": 6,
    "reminders_respect_work_hours": True,
    "appointment_reminder_minutes": 30,
    "client_followup_days": 7,  # عميل بلا متابعة بعد عدد الأيام هذا يظهر في التقارير
    "auto_reply_enabled": True,  # الردود المعتمدة فقط
    "ack_unknown_messages": True,  # رد استلام مهذب للرسائل الجديدة على رقم نوف
    "owner_inbox_auto_reply": False,  # لا رد تلقائي على رقم المالك (Coexistence) افتراضيًا
    "reply_tone": "سعودي رسمي راقٍ، مختصر ولبق، بدون مبالغة",
    "company_name": "الشركة",
    "proof_required_by_default": False,
}


def get_setting(db: Session, key: str):
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return row.value


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings(db: Session) -> dict:
    out = dict(DEFAULTS)
    for row in db.query(Setting).all():
        out[row.key] = row.value
    return out
