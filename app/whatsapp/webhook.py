"""تحويل حمولة Webhook من Meta إلى رسائل موحّدة."""
from dataclasses import dataclass, field


@dataclass
class Inbound:
    wa_message_id: str
    phone: str
    phone_number_id: str
    msg_type: str
    text: str | None = None
    media_id: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    context_wa_id: str | None = None
    interactive_payload: str | None = None
    profile_name: str | None = None
    timestamp: str | None = None
    is_echo: bool = False  # رسالة أرسلها المالك من تطبيق WhatsApp Business (Coexistence)
    raw: dict = field(default_factory=dict)


@dataclass
class StatusUpdate:
    wa_message_id: str
    status: str
    recipient: str | None
    error: str | None = None


def _parse_message(m: dict, phone_number_id: str, contacts: dict, echo: bool = False) -> Inbound:
    t = m.get("type", "text")
    phone = m.get("to") if echo else m.get("from")
    ib = Inbound(wa_message_id=m["id"], phone=phone, phone_number_id=phone_number_id, msg_type=t,
                 context_wa_id=(m.get("context") or {}).get("id"), profile_name=contacts.get(m.get("from")),
                 timestamp=m.get("timestamp"), is_echo=echo, raw=m)
    if t == "text":
        ib.text = m["text"]["body"]
    elif t in ("image", "audio", "voice", "video", "document", "sticker"):
        media = m.get(t, {})
        ib.media_id = media.get("id")
        ib.media_mime = media.get("mime_type")
        ib.media_filename = media.get("filename")
        ib.text = media.get("caption")
        if t == "voice":
            ib.msg_type = "audio"
    elif t == "interactive":
        it = m["interactive"]
        if it.get("type") == "button_reply":
            ib.interactive_payload = it["button_reply"]["id"]
            ib.text = it["button_reply"]["title"]
        elif it.get("type") == "list_reply":
            ib.interactive_payload = it["list_reply"]["id"]
            ib.text = it["list_reply"]["title"]
    elif t == "button":  # زر قالب (quick reply)
        ib.interactive_payload = m["button"].get("payload")
        ib.text = m["button"].get("text")
    elif t == "location":
        loc = m["location"]
        ib.text = f"موقع: {loc.get('name') or ''} {loc.get('latitude')},{loc.get('longitude')}"
    elif t == "contacts":
        ib.text = "جهة اتصال: " + ", ".join(c.get("name", {}).get("formatted_name", "") for c in m["contacts"])
    return ib


def parse_webhook(payload: dict) -> tuple[list[Inbound], list[StatusUpdate]]:
    msgs: list[Inbound] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field_name = change.get("field")
            value = change.get("value", {})
            pnid = (value.get("metadata") or {}).get("phone_number_id", "")
            contacts = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}
            if field_name == "messages":
                for m in value.get("messages", []):
                    msgs.append(_parse_message(m, pnid, contacts))
                for s in value.get("statuses", []):
                    err = None
                    if s.get("errors"):
                        e = s["errors"][0]
                        err = f"{e.get('code')}: {e.get('title')}"
                    statuses.append(StatusUpdate(s["id"], s.get("status"), s.get("recipient_id"), err))
            elif field_name == "smb_message_echoes":
                for m in value.get("message_echoes", []):
                    msgs.append(_parse_message(m, pnid, contacts, echo=True))
    return msgs, statuses
