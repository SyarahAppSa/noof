"""كل رسالة صادرة تمر من هنا: تُسجَّل ثم تُرسل عبر الطابور مع مراعاة نافذة 24 ساعة.

قواعد Meta: الرسائل الحرة مسموحة فقط خلال 24 ساعة من آخر رسالة واردة من الشخص.
خارج النافذة: نرسل قالبًا معتمدًا (Utility) أو نؤجل الرسالة ونرسل قالب "لديك تحديثات"،
وعند رد الشخص تُفرَّغ الرسائل المؤجلة تلقائيًا.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Message
from app.queue import enqueue
from app.timeutil import utcnow
from app.whatsapp.client import REENGAGEMENT_CODES, WhatsAppError, get_client

WINDOW = timedelta(hours=24)

TEMPLATES = {
    "task_assigned": "noof_task_assigned",  # {{1}} الاسم {{2}} المهمة {{3}} الموعد {{4}} الرمز
    "task_reminder": "noof_task_reminder",  # {{1}} الرمز {{2}} المهمة {{3}} الموعد
    "staff_update": "noof_staff_update",  # {{1}} الاسم — "لديك تحديثات من نوف"
    "owner_update": "noof_owner_update",  # {{1}} عدد التحديثات
    "appointment_confirmation": "noof_appointment_confirmation",  # {{1}} الاسم {{2}} الموعد
    "client_followup": "noof_client_followup",  # {{1}} الاسم {{2}} الموضوع
}


def assistant_channel() -> str:
    return get_settings().wa_phone_number_id or "assistant"


def window_open(db: Session, phone: str, channel: str | None = None) -> bool:
    channel = channel or assistant_channel()
    last = db.scalar(
        select(Message.created_at)
        .where(Message.phone == phone, Message.direction == "in", Message.channel_phone_number_id == channel)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return bool(last and utcnow() - last < WINDOW)


def send(db: Session, to: str, body: str, *, buttons: list[tuple[str, str]] | None = None,
         template: tuple[str, list[str]] | None = None, template_buttons: list[str] | None = None,
         user_id: int | None = None, contact_id: int | None = None, client_id: int | None = None,
         task_id: int | None = None, reply_to: str | None = None, channel: str | None = None,
         category: str | None = None) -> Message:
    """يسجّل رسالة صادرة ويضعها في الطابور. template = (مفتاح القالب, المعاملات) يُستخدم خارج النافذة."""
    msg = Message(direction="out", phone=to, body=body, user_id=user_id, contact_id=contact_id, client_id=client_id,
                  task_id=task_id, msg_type="interactive" if buttons else "text", status="queued",
                  channel_phone_number_id=channel or assistant_channel(), category=category,
                  raw={"buttons": buttons or [], "template": list(template) if template else None,
                       "template_buttons": template_buttons or [], "reply_to": reply_to})
    db.add(msg)
    db.flush()
    enqueue(db, "send_message", {"message_id": msg.id})
    return msg


def deliver(db: Session, msg: Message) -> None:
    """ينفذه العامل الخلفي: إرسال فعلي مع اختيار القالب عند الحاجة."""
    client = get_client(msg.channel_phone_number_id)
    meta = msg.raw or {}
    template = meta.get("template")
    open_ = window_open(db, msg.phone, msg.channel_phone_number_id)
    try:
        if open_:
            if meta.get("buttons"):
                res = client.send_buttons(msg.phone, msg.body, [tuple(b) for b in meta["buttons"]])
            else:
                res = client.send_text(msg.phone, msg.body, reply_to=meta.get("reply_to"))
        elif template:
            key, params = template
            msg.template_name = TEMPLATES.get(key, key)
            res = client.send_template(msg.phone, msg.template_name, params, button_payloads=meta.get("template_buttons"))
        else:
            _defer(db, msg)
            return
    except WhatsAppError as e:
        if e.code in REENGAGEMENT_CODES and not template:
            _defer(db, msg)
            return
        msg.status = "failed"
        msg.error = str(e)
        if e.retryable:
            raise
        return
    msg.wa_message_id = res.wa_message_id
    msg.status = "dry_run" if res.dry_run else "sent"


def _defer(db: Session, msg: Message) -> None:
    """خارج نافذة 24 ساعة وبدون قالب مخصص: نؤجل الرسالة ونرسل تنبيه قالب واحد."""
    from app.models import User
    from app.services import directory

    msg.status = "deferred"
    owner = directory.owner_phone()
    pending = db.query(Message).filter(Message.phone == msg.phone, Message.status == "deferred").count()
    if msg.phone == owner:
        nudge = ("owner_update", [str(pending + 1)])
        payloads = ["owner:flush"]
    else:
        u = db.scalar(select(User).where(User.phone == msg.phone))
        nudge = ("staff_update", [u.name if u else "عزيزنا"])
        payloads = ["staff:flush"]
    from app.queue import enqueue as _enq
    bucket = utcnow().strftime("%Y%m%d%H")
    _enq(db, "send_template_nudge", {"phone": msg.phone, "template": list(nudge), "payloads": payloads,
                                     "channel": msg.channel_phone_number_id},
         dedupe_key=f"nudge:{msg.phone}:{bucket}")


def flush_deferred(db: Session, phone: str) -> int:
    rows = db.query(Message).filter(Message.phone == phone, Message.status == "deferred").order_by(Message.id).all()
    for m in rows:
        m.status = "queued"
        enqueue(db, "send_message", {"message_id": m.id})
    return len(rows)
