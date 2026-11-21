"""نقطة استقبال Webhook الرسمية من Meta: تتحقق من التوقيع، تحفظ، وتضع في الطابور، وترد 200 فورًا."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Message
from app.queue import enqueue
from app.security import verify_meta_signature
from app.whatsapp.webhook import Inbound, parse_webhook

router = APIRouter()
log = logging.getLogger("noof.webhook")


@router.get("/webhook/whatsapp")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == get_settings().wa_verify_token \
            and get_settings().wa_verify_token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


def store_inbound(db: Session, ib: Inbound) -> Message | None:
    if db.scalar(select(Message.id).where(Message.wa_message_id == ib.wa_message_id)):
        return None  # Meta قد تعيد الإرسال — نتجاهل المكرر
    raw = dict(ib.raw)
    raw["_profile_name"] = ib.profile_name
    msg = Message(wa_message_id=ib.wa_message_id, direction="echo" if ib.is_echo else "in", phone=ib.phone,
                  channel_phone_number_id=ib.phone_number_id, msg_type=ib.msg_type, body=ib.text,
                  media_id=ib.media_id, media_mime=ib.media_mime, media_filename=ib.media_filename,
                  context_wa_id=ib.context_wa_id, interactive_payload=ib.interactive_payload, raw=raw)
    try:
        with db.begin_nested():
            db.add(msg)
            db.flush()
    except IntegrityError:
        return None
    enqueue(db, "process_inbound", {"message_id": msg.id})
    return msg


@router.post("/webhook/whatsapp")
async def receive(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_meta_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid json")
    msgs, statuses = parse_webhook(payload)
    for ib in msgs:
        store_inbound(db, ib)
    for st in statuses:
        m = db.scalar(select(Message).where(Message.wa_message_id == st.wa_message_id))
        if m:
            m.status = st.status
            if st.error:
                m.error = st.error
    db.commit()
    return {"ok": True}
