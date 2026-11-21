"""الأوامر الصوتية، وصندوق رقم المالك عبر Coexistence (بدون رد تلقائي)."""
import itertools

from sqlalchemy import select

from app.models import Client, Message, Task

OWNER = "966500000001"
_ids = itertools.count(1)


def _audio(phone, pnid="PN_NOOF"):
    m = {"from": phone, "id": f"wamid.voice.{next(_ids)}", "type": "audio", "audio": {"id": "AUD1", "mime_type": "audio/ogg; codecs=opus", "voice": True}}
    return {"entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": pnid},
            "contacts": [{"wa_id": phone, "profile": {"name": "x"}}], "messages": [m]}}]}]}


def test_owner_voice_note_becomes_command(team, wa, send, db, monkeypatch):
    from app.ai import stt
    monkeypatch.setattr(stt, "transcribe", lambda data, mime: "أرسل لسارة مراجعة الفواتير بكرة الساعة 10")
    send(_audio(OWNER))
    t = db.scalars(select(Task)).first()
    assert t and t.assignee_id == team["sara"].id and "مراجعة الفواتير" in t.title
    msg = db.scalars(select(Message).where(Message.msg_type == "audio")).first()
    assert msg.extracted_text.startswith("أرسل لسارة")


def test_owner_inbox_coexistence_no_auto_reply_and_echo(team, wa, send, db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "wa_owner_inbox_phone_number_id", "PN_OWNER")
    client_phone = "966566666666"
    payload = {"entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "PN_OWNER"},
               "contacts": [{"wa_id": client_phone, "profile": {"name": "عميل"}}],
               "messages": [{"from": client_phone, "id": "wamid.coex.1", "type": "text",
                             "text": {"body": "عندي شكوى بخصوص الفاتورة"}}]}}]}]}
    send(payload)
    assert wa.to(client_phone) == []  # لا رد تلقائي على رقم المالك
    assert db.scalars(select(Message).where(Message.phone == client_phone)).first().needs_owner
    echo = {"entry": [{"changes": [{"field": "smb_message_echoes", "value": {"metadata": {"phone_number_id": "PN_OWNER"},
            "message_echoes": [{"from": "966500000001", "to": client_phone, "id": "wamid.echo.1", "type": "text",
                                "text": {"body": "أبشر، نتابع الموضوع"}}]}}]}]}
    send(echo)
    db.expire_all()
    c = db.scalars(select(Client).where(Client.phone == client_phone)).first()
    assert c.last_followup_at is not None
