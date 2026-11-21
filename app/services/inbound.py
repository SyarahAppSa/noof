"""معالجة كل رسالة واردة: التعرف على المرسل، الوسائط، التصنيف، والتوجيه."""
import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import llm, stt
from app.audit import audit
from app.config import get_settings
from app.models import (
    Approval,
    ApprovalKind,
    AutoReplyRule,
    Client,
    Contact,
    Message,
    Role,
    StaffRequest,
    Task,
    TaskAttachment,
    User,
)
from app.nlp.arabic import normalize
from app.nlp.dates import parse_when
from app.services import appointments as appt_svc
from app.services import approvals as appr_svc
from app.services import clients as client_svc
from app.services import commands_exec, directory
from app.services import tasks as task_svc
from app.services.settings import get_setting
from app.timeutil import fmt, utcnow
from app.whatsapp import outbox
from app.whatsapp.client import WhatsAppError, get_client

log = logging.getLogger("noof.inbound")


def _re(pattern: str, flags: int = 0) -> re.Pattern:
    """تُوحَّد الأنماط بنفس توحيد النص (أ/إ→ا، ى→ي...) لتتطابق دائمًا."""
    return re.compile(normalize(pattern), flags)


DONE_RE = _re(r"^\s*(تم|خلصت|خلصنا|انتهيت|انتهينا|انجزت|انجزتها|تم الانجاز|تم الانتهاء|خلاص خلصت|done|خلصتها)\b", re.I)
ACK_RE = _re(r"^\s*(استلمت|تم الاستلام|وصلت|وصلني|ابشر|ابشري|حاضر|تمام|ان شاء الله|إن شاء الله|ok|اوكي|طيب)\b", re.I)
START_RE = _re(r"^\s*(جاري|جار|بديت|بدأت|بدات|شغال|قاعد اشتغل|تحت التنفيذ|اشتغل عليها)\b")
EXTEND_RE = _re(r"(تمديد|امهلني|أمهلني|احتاج وقت|أحتاج وقت|ابي وقت|أبي وقت|اقدر اسلمها|أقدر أسلمها|اخرها|أأجلها|تأجيل)")
MY_TASKS_RE = _re(r"^\s*(مهامي|وش مهامي|ايش مهامي|ما هي مهامي|المهام)\s*\??$")

COMPLAINT_RE = _re(r"(شكوى|اشتكي|سيء|سيئ|زعلان|غير راض|مو راضي|ما عجبني|مشكله|مشكلة|تأخرتو|تاخرتو|متأخرين|اسوأ|خايس|نصب|مستاء)")
PRICE_RE = _re(r"(سعر|اسعار|أسعار|عرض سعر|تسعيره|تسعيرة|بكم|كم السعر|كم يكلف|تكلفه|تكلفة|خصم)")
APPT_RE = _re(r"(موعد|اجتماع|زياره|زيارة|نلتقي|مقابله|مقابلة|ميتنق)")
URGENT_RE = _re(r"(عاجل|ضروري|طارئ|مستعجل|بسرعه|بسرعة|فورا|فوراً)")
SUPPLIER_RE = _re(r"(توريد|مورد|شحنه|شحنة|فاتوره|فاتورة المورد|بضاعه|بضاعة)")


def _is_assistant_channel(pnid: str) -> bool:
    s = get_settings()
    return not s.wa_owner_inbox_phone_number_id or pnid != s.wa_owner_inbox_phone_number_id


def process(db: Session, msg: Message) -> None:
    """نقطة الدخول من العامل الخلفي لرسالة واردة محفوظة مسبقًا."""
    if msg.handled:
        return
    if msg.direction == "echo":
        _handle_echo(db, msg)
        msg.handled = True
        return
    user = directory.user_by_phone(db, msg.phone)
    if user:
        msg.user_id = user.id
    if msg.channel_phone_number_id and _is_assistant_channel(msg.channel_phone_number_id):
        # فتح نافذة 24 ساعة: نفرغ أي رسائل مؤجلة لهذا الشخص
        outbox.flush_deferred(db, msg.phone)
    _enrich_media(db, msg, transcribe_for_command=bool(user))
    text = msg.extracted_text if msg.msg_type == "audio" and msg.extracted_text else msg.body

    if msg.interactive_payload:
        reply = _handle_payload(db, msg, user)
        if reply:
            _reply(db, msg, reply, user)
        msg.handled = True
        return

    if user and _is_assistant_channel(msg.channel_phone_number_id or ""):
        msg.category = "command" if user.role in (Role.owner, Role.manager) else "employee"
        reply = _handle_staff(db, msg, user, text or "")
        if reply:
            _reply(db, msg, reply, user)
        msg.handled = True
        return

    _handle_external(db, msg, text or "")


def _reply(db: Session, msg: Message, text: str, user: User | None):
    outbox.send(db, msg.phone, text, user_id=user.id if user else None, reply_to=msg.wa_message_id,
                channel=msg.channel_phone_number_id)


# ---------- الوسائط ----------

def _enrich_media(db: Session, msg: Message, transcribe_for_command: bool):
    if not msg.media_id or msg.extracted_text:
        return
    try:
        data, mime = get_client(msg.channel_phone_number_id).download_media(msg.media_id)
    except (WhatsAppError, Exception) as e:  # noqa: BLE001 — نكمل المعالجة حتى لو فشل التحميل
        log.warning("media download failed: %s", e)
        return
    msg.media_mime = msg.media_mime or mime
    if msg.msg_type == "audio":
        txt = stt.transcribe(data, mime)
        msg.extracted_text = txt
        if txt:
            audit(db, "media.transcribed", entity_type="message", entity_id=msg.id)
    elif msg.msg_type in ("image", "document"):
        r = llm.extract_media(data, mime, msg.body)
        if r:
            msg.extracted_text = r["text"][:8000]
            msg.summary = f"{r['doc_type']}: {r['summary']}"
            if r["amounts"] or r["dates"]:
                msg.summary += f" | مبالغ: {', '.join(r['amounts'])} | تواريخ: {', '.join(r['dates'])}"


# ---------- الأزرار ----------

def _handle_payload(db: Session, msg: Message, user: User | None) -> str | None:
    p = msg.interactive_payload or ""
    parts = p.split(":")
    if p in ("owner:flush", "staff:flush"):
        n = outbox.flush_deferred(db, msg.phone)
        return None if n else "لا توجد تحديثات جديدة حاليًا ✅"
    if not user:
        return None
    try:
        if parts[0] == "task" and len(parts) == 3:
            task = db.get(Task, int(parts[2]))
            if not task:
                return "لم أجد المهمة."
            action = parts[1]
            if action == "ack":
                return task_svc.acknowledge(db, task, user)
            if action == "start":
                return task_svc.start(db, task, user)
            if action == "done":
                return task_svc.submit_done(db, task, user)
        if parts[0] == "appr" and len(parts) == 3:
            ap = db.get(Approval, int(parts[2]))
            if not ap:
                return "لم أجد طلب الموافقة."
            return appr_svc.decide(db, ap, user, parts[1] == "yes")
    except (task_svc.TaskError, appr_svc.ApprovalError) as e:
        return f"⚠️ {e}"
    return None


# ---------- الفريق ----------

def _handle_staff(db: Session, msg: Message, user: User, text: str) -> str | None:
    orig = text or ""
    text = normalize(orig)
    state = task_svc.get_state(db, user.phone)
    if state and state.kind == "awaiting_proof":
        task = db.get(Task, state.data["task_id"])
        if msg.media_id and task:
            return task_svc.submit_done(db, task, user, note=state.data.get("note") or msg.body,
                                        attachments=[_att(msg)])
        if text and re.search(r"(الغاء|إلغاء|لاحقا|لاحقًا|بعدين)", text):
            task_svc.clear_state(db, user.phone)
            return "تمام، أرسل الإثبات متى ما جهز مع كلمة «تم»."

    if user.role in (Role.owner, Role.manager) and not DONE_RE.match(text or "") and not ACK_RE.match(text or ""):
        return commands_exec.handle(db, user, orig, source_message_id=msg.id)

    # موظف (أو مدير يحدّث مهامه الخاصة)
    if MY_TASKS_RE.match(text or ""):
        ts = task_svc.open_tasks_for(db, user)
        if not ts:
            return "لا توجد لديك مهام مفتوحة حاليًا ✅"
        return "📋 *مهامك المفتوحة:*\n" + "\n".join(
            f"• {t.code} {t.title} — {task_svc.STATUS_AR[t.status]} — الموعد: {fmt(t.due_at)}" for t in ts)

    is_done = bool(DONE_RE.match(text or "")) or (msg.media_id and re.search(r"\bتم\b", msg.body or ""))
    for regex, action in ((DONE_RE, "done"), (START_RE, "start"), (ACK_RE, "ack")):
        if (action == "done" and is_done) or (action != "done" and regex.match(text or "")):
            cands = task_svc.resolve_task_for_reply(db, user, text, msg.context_wa_id)
            if action == "ack":
                cands = [t for t in cands if t.acknowledged_at is None] or cands
            if not cands:
                if action == "ack":
                    return None if not task_svc.open_tasks_for(db, user) else "شكرًا لك 🌷"
                return "لا توجد لديك مهام مفتوحة مطابقة."
            if len(cands) > 1:
                return ("لديك أكثر من مهمة مفتوحة، أيّها تقصد؟ أرسل الكلمة مع رقم المهمة، مثل «تم T-12»:\n"
                        + "\n".join(f"• {t.code} {t.title}" for t in cands))
            task = cands[0]
            if action == "done":
                note = DONE_RE.sub("", text or "").strip() or None
                return task_svc.submit_done(db, task, user, note=note, attachments=[_att(msg)] if msg.media_id else None)
            if action == "start":
                return task_svc.start(db, task, user)
            return task_svc.acknowledge(db, task, user)

    if msg.media_id:
        cands = task_svc.resolve_task_for_reply(db, user, text, msg.context_wa_id)
        if len(cands) == 1:
            db.add(TaskAttachment(
                task_id=cands[0].id, message_id=msg.id, media_id=msg.media_id, mime=msg.media_mime,
                filename=msg.media_filename, caption=msg.body))
            return f"تم إرفاق الملف بالمهمة {cands[0].code} 📎 أرسل «تم» عند الانتهاء."

    if EXTEND_RE.search(text or ""):
        cands = task_svc.resolve_task_for_reply(db, user, text, msg.context_wa_id)
        when = parse_when(text, work_end=get_setting(db, "work_end"))
        if len(cands) == 1:
            t = cands[0]
            ap = appr_svc.request(db, ApprovalKind.deadline_extension,
                                  f"{user.name} يطلب تمديد {t.code} «{t.title}» من {fmt(t.due_at)} إلى {fmt(when.dt) if when.dt else 'غير محدد'}",
                                  {"task_id": t.id, "new_due": when.dt.isoformat() if when.dt else None},
                                  requested_by=user, task_id=t.id, details=f"نص الطلب: {orig}")
            return f"تم رفع طلب التمديد ({ap.code}) للإدارة، وسأبلغك بالقرار."

    # طلب عام من موظف
    req = StaffRequest(user_id=user.id, text=orig or "(مرفق)", message_id=msg.id,
                       category="urgent" if URGENT_RE.search(text or "") else "general")
    db.add(req)
    db.flush()
    if req.category == "urgent":
        owner = directory.get_owner(db)
        targets = {owner.phone: owner} if owner else {}
        if user.manager_id:
            mgr = db.get(User, user.manager_id)
            if mgr:
                targets[mgr.phone] = mgr
        for ph, u in targets.items():
            if ph != user.phone:
                outbox.send(db, ph, f"📨 طلب عاجل من {user.name}:\n{orig}", user_id=u.id)
    return "تم تسجيل طلبك ✅ وسيتم الرد عليك في أقرب وقت."


def _att(msg: Message) -> dict:
    return {"message_id": msg.id, "media_id": msg.media_id, "mime": msg.media_mime, "filename": msg.media_filename,
            "caption": msg.body}


# ---------- الأطراف الخارجية ----------

def _rule_classify(text: str) -> dict:
    t = normalize(text)
    cat, urgency, sentiment, needs_owner = "client", "normal", "neutral", False
    if COMPLAINT_RE.search(t):
        cat, sentiment, needs_owner, urgency = "complaint", "negative", True, "high"
    elif PRICE_RE.search(t):
        cat = "sales_opportunity"
    elif APPT_RE.search(t):
        cat = "appointment"
    elif SUPPLIER_RE.search(t):
        cat = "supplier"
    if URGENT_RE.search(t):
        urgency, needs_owner = "high", True
        if cat == "client":
            cat = "urgent"
    draft = None
    if cat == "complaint":
        draft = ("نعتذر لكم عن أي إزعاج، ووصلتنا ملاحظتكم باهتمام. سيتواصل معكم المسؤول شخصيًا في أقرب وقت "
                 "لمعالجة الموضوع بإذن الله.")
    elif needs_owner or PRICE_RE.search(t):
        draft = "شكرًا لتواصلكم، وصلنا طلبكم وسيتواصل معكم المختص لتزويدكم بالتفاصيل الدقيقة في أقرب وقت."
    return {"category": cat, "urgency": urgency, "sentiment": sentiment, "summary": text[:160],
            "needs_owner": needs_owner, "is_sales_opportunity": cat == "sales_opportunity", "person_name": None,
            "company": None, "sector": None, "wants_appointment": cat == "appointment", "appointment_text": text,
            "involves_price_or_commitment": bool(PRICE_RE.search(t)), "suggested_reply": draft, "faq_rule_id": None}


def _faq_match(db: Session, text: str) -> AutoReplyRule | None:
    t = normalize(text)
    for r in db.scalars(select(AutoReplyRule).where(AutoReplyRule.enabled.is_(True))).all():
        if r.keywords and any(normalize(k) in t for k in r.keywords):
            return r
    return None


def _handle_external(db: Session, msg: Message, text: str):
    contact = directory.upsert_contact(db, msg.phone, (msg.raw or {}).get("_profile_name"))
    msg.contact_id = contact.id
    contact.last_inbound_at = utcnow()
    on_assistant = _is_assistant_channel(msg.channel_phone_number_id or "")
    content = text or msg.summary or ""
    if msg.msg_type == "audio" and not msg.extracted_text:
        content = content or "(رسالة صوتية لم يتم تحويلها لنص)"

    history = [f"{'هم' if m.direction == 'in' else 'نحن'}: {m.body or ''}" for m in db.scalars(
        select(Message).where(Message.phone == msg.phone, Message.id != msg.id).order_by(Message.id.desc()).limit(6)).all()][::-1]
    faqs = [{"id": r.id, "name": r.name, "answer": r.answer}
            for r in db.scalars(select(AutoReplyRule).where(AutoReplyRule.enabled.is_(True))).all()]
    cls = llm.classify_inbound(content, sender=contact.name or contact.phone, history=history, faqs=faqs,
                               tone=get_setting(db, "reply_tone")) or _rule_classify(content)

    msg.category = cls["category"]
    msg.urgency = cls["urgency"]
    msg.sentiment = cls["sentiment"]
    msg.summary = msg.summary or cls["summary"]
    if cls["category"] == "supplier":
        contact.kind = "supplier"
    elif cls["category"] == "personal":
        contact.kind = contact.kind if contact.kind != "unknown" else "personal"

    client = None
    if cls["category"] not in ("personal", "supplier"):
        client = client_svc.ensure_client_for_contact(db, contact, name=cls.get("person_name"),
                                                      company=cls.get("company"), sector=cls.get("sector"))
        msg.client_id = client.id
        client_svc.touch_contact(client)
        if cls["is_sales_opportunity"]:
            client_svc.create_opportunity(db, client, cls["summary"][:200] or "فرصة من واتساب", source_message_id=msg.id)

    vip = bool(client and client.is_vip)
    needs_owner = bool(cls["needs_owner"] or vip or cls["sentiment"] == "angry" or cls["category"] in ("complaint", "urgent")
                       or cls["involves_price_or_commitment"])
    msg.needs_owner = needs_owner
    audit(db, "message.classified", actor_type="contact", actor_id=contact.id, entity_type="message", entity_id=msg.id,
          details={"category": msg.category, "needs_owner": needs_owner})

    # طلب موعد: يُسجل مبدئيًا ولا يُؤكد إلا بعد موافقة المالك
    if cls["wants_appointment"] and on_assistant:
        when = parse_when(cls.get("appointment_text") or content)
        if when.dt and when.has_time:
            appt_svc.create(db, title=f"موعد مع {client.name if client else contact.phone}", start_at=when.dt,
                            client=client, person_phone=contact.phone, person_name=contact.name)

    # الرد التلقائي: فقط من الردود المعتمدة وفي الحالات الواضحة
    auto_allowed = on_assistant or get_setting(db, "owner_inbox_auto_reply")
    replied = False
    if auto_allowed and get_setting(db, "auto_reply_enabled") and cls["category"] not in ("complaint", "personal") \
            and not cls["involves_price_or_commitment"] and cls["sentiment"] != "angry":
        rule = None
        if cls.get("faq_rule_id"):
            rule = db.get(AutoReplyRule, cls["faq_rule_id"])
            rule = rule if rule and rule.enabled else None
        rule = rule or _faq_match(db, content)
        if rule:
            outbox.send(db, msg.phone, rule.answer, contact_id=contact.id, client_id=msg.client_id,
                        reply_to=msg.wa_message_id, channel=msg.channel_phone_number_id, category="auto_reply")
            audit(db, "message.auto_reply", entity_type="message", entity_id=msg.id, details={"rule": rule.id})
            replied = True
    if not replied and on_assistant and get_setting(db, "ack_unknown_messages") and cls["category"] != "personal" and (
            not contact.last_ack_at or utcnow() - contact.last_ack_at > timedelta(hours=12)):
        company = get_setting(db, "company_name")
        outbox.send(db, msg.phone,
                    f"أهلًا وسهلًا، شكرًا لتواصلكم مع {company} 🌷\nتم استلام رسالتكم وسيتواصل معكم المختص في أقرب وقت بإذن الله.",
                    contact_id=contact.id, client_id=msg.client_id, channel=msg.channel_phone_number_id, category="ack")
        contact.last_ack_at = utcnow()

    if needs_owner:
        _alert_owner(db, msg, contact, client, cls)
    msg.handled = True


def _alert_owner(db: Session, msg: Message, contact: Contact, client, cls: dict):
    owner = directory.get_owner(db)
    if not owner:
        return
    label = {"complaint": "⚠️ شكوى", "urgent": "🚨 طلب عاجل", "sales_opportunity": "💼 فرصة مبيعات",
             "appointment": "🗓️ طلب موعد"}.get(cls["category"], "💬 رسالة مهمة")
    who = f"{contact.name or ''} ({contact.phone})".strip()
    if client and client.is_vip:
        label += " — عميل مهم ⭐"
    draft = cls.get("suggested_reply")
    body = f"{label}\nمن: {who}\nالملخص: {cls['summary']}\nالنص: {(msg.body or msg.extracted_text or '')[:500]}"
    if draft:
        ap = appr_svc.request(db, ApprovalKind.send_message, f"رد على {who}",
                              {"to": contact.phone, "text": draft, "contact_id": contact.id,
                               "channel": msg.channel_phone_number_id},
                              client_id=client.id if client else None, source_message_id=msg.id,
                              details=f"{body}\n\n✍️ *الرد المقترح:*\n{draft}\n\nلإرسال رد مختلف: «رد {{code}}: نص الرد»")
        audit(db, "owner.alerted", entity_type="message", entity_id=msg.id, details={"approval": ap.id})
    else:
        outbox.send(db, owner.phone, body + "\n\nللرد اكتب ردك وسأجهزه للإرسال.", user_id=owner.id)
        audit(db, "owner.alerted", entity_type="message", entity_id=msg.id)


def _handle_echo(db: Session, msg: Message):
    """رسالة أرسلها المالك بنفسه من تطبيق WhatsApp Business (Coexistence): نحدّث آخر متابعة للعميل."""
    contact = directory.upsert_contact(db, msg.phone, None)
    msg.contact_id = contact.id
    if contact.client_id:
        c = db.get(Client, contact.client_id)
        if c:
            client_svc.touch_contact(c, outbound=True)
            msg.client_id = c.id
