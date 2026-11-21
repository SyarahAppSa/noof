"""محلل أوامر المالك/المدير بالقواعد (سريع، مجاني، حتمي). عند عدم الوضوح يُستخدم Claude."""
import re
from dataclasses import dataclass, field

from app.models import Priority
from app.nlp.arabic import normalize


@dataclass
class Intent:
    name: str
    slots: dict = field(default_factory=dict)
    confident: bool = True


ASSIGN_VERBS = r"(ارسل|ابعث|بلغ|كلف|اطلب من|قول|قل|خل|خلي|وجه|اعط|عط|اسند|ذكر)"
URGENT_WORDS = r"(عاجل|ضروري|مستعجل|فوري|حالا|بسرعه|بسرعة|طارئ)"
HIGH_WORDS = r"(مهم جدا|اولويه عاليه|اولوية عالية|مهم)"
PROOF_WORDS = r"(مع اثبات|مع صوره|مع صورة|مع ملف|ارفق|يرسل الملف|بالاثبات|مع الاثبات|مع ارفاق)"


def detect_priority(t: str) -> Priority:
    if re.search(URGENT_WORDS, t):
        return Priority.urgent
    if re.search(HIGH_WORDS, t):
        return Priority.high
    return Priority.normal


def parse(text: str) -> Intent:
    t = normalize(text)
    tl = t.lower()

    m = re.match(r"^(موافق|اعتمد|اوافق|نعم اعتمد)\s*(?:علي)?\s*([aA]\s*-?\s*\d+)", tl)
    if m:
        return Intent("approve", {"code": re.sub(r"\D", "", m.group(2))})
    m = re.match(r"^رد\s*(?:علي)?\s*([aA]\s*-?\s*\d+)\s*[:：\-]?\s*(.+)$", t, re.S)
    if m:
        return Intent("approve", {"code": re.sub(r"\D", "", m.group(1)), "override_text": text.split(":", 1)[-1].strip()
                                  if ":" in text else m.group(2).strip()})
    m = re.match(r"^(ارفض|رفض|لا توافق|مرفوض)\s*([aA]\s*-?\s*\d+)(.*)$", tl)
    if m:
        return Intent("reject", {"code": re.sub(r"\D", "", m.group(2)), "note": m.group(3).strip() or None})
    m = re.search(r"(?:الغ|إلغ|الغي|الغاء)\s*(?:المهمه|المهمة)?\s*[tT]\s*-?\s*(\d+)", tl)
    if m:
        return Intent("cancel_task", {"task_id": int(m.group(1))})
    m = re.search(r"(?:حاله|حالة|وين|وش صار|ايش صار)\s*(?:في|ب)?\s*(?:المهمه|المهمة)?\s*[tT]\s*-?\s*(\d+)", tl)
    if m:
        return Intent("task_status", {"task_id": int(m.group(1))})

    if re.search(r"^(مساعده|مساعدة|الاوامر|help|\?)$", tl):
        return Intent("help")
    if re.search(r"(يحتاج|تحتاج|ينتظر|بانتظار).{0,15}(قراري|موافقتي|قرار|اعتمادي)|(اهم|ايش|وش|ما).{0,20}(قراري)", t):
        return Intent("decisions_needed")
    if re.search(r"(مهام|مهمات|اعمال).{0,10}(متاخره|متأخرة|متاخرة|متأخره)|(متاخر|متأخر).{0,10}(مهام|مهمات)|مين متاخر|من متاخر", t):
        return Intent("list_overdue")
    if re.search(r"^(?:(?:ارسل|ابعث|اعطني|عطني|ابي|ابغي|ابغى|وريني|هات|جهز|طلع)\s+)?(?:لي\s+)?(?:ال)?تقرير", t):
        kind = "summary"
        if re.search(r"الموظفين|الفريق|الموظف", t):
            kind = "employees"
        elif re.search(r"صباح", t):
            kind = "morning"
        elif re.search(r"مساء|مسائي|اليوم", t):
            kind = "evening"
        elif re.search(r"العملاء|المبيعات|الفرص", t):
            kind = "clients"
        return Intent("send_report", {"kind": kind})
    if re.search(r"(العملاء|الفرص|عملاء).{0,30}(لم|ما|بدون|تحتاج|يحتاجون).{0,15}(نتابع|متابعه|متابعة|نتابعهم|تابعنا)", t) or \
            re.search(r"(اهم|ابرز) (العملاء|الفرص)", t):
        return Intent("clients_followup")
    if re.search(r"^(مهامي|مهام الفريق|المهام المفتوحه|المهام المفتوحة|وش المهام|ايش المهام)", t):
        return Intent("list_tasks")

    if re.search(r"(موعد|اجتماع|ميتنق|زياره|زيارة)", t) and re.search(r"(اضف|ضيف|سجل|حدد|احجز|رتب|ثبت|نسق)", t):
        m = re.search(r"مع\s+(?:العميل|الاستاذ|الأستاذ|شركه|شركة|الدكتور|المهندس|الشيخ)?\s*([^\s,]+(?:\s+[^\s,]+)?)", t)
        name = m.group(1) if m else None
        if name:
            name = re.split(r"\s(يوم|بكره|بكرة|غدا|اليوم|الساعه|الساعة|الاحد|الاثنين|الثلاثاء|الاربعاء|الخميس|الجمعه|السبت|بخصوص|عن)\b", " " + name)[0].strip() or name
        subj = re.search(r"(?:بخصوص|عن|لمناقشه|لمناقشة|موضوع)\s+(.+)$", t)
        return Intent("create_appointment", {"person": name, "subject": subj.group(1) if subj else None, "raw": text})

    m = re.search(r"تابع\s+مع\s+(.+)$", t)
    if m:
        rest = m.group(1)
        every = re.search(r"كل\s+(\d+(?:\.\d+)?)?\s*(ساعه|ساعة|ساعات|ساعتين|يوم|ايام|يومين)", rest)
        hours = None
        if every:
            n = float(every.group(1)) if every.group(1) else (2 if every.group(2) in ("ساعتين", "يومين") else 1)
            hours = n * 24 if every.group(2).startswith(("يوم", "ايام")) else n
        subj = re.search(r"(?:بخصوص|عن|في موضوع|حول)\s+(.+?)(?:\s+كل\s+\d|\s+حتي|\s+لين|$)", rest)
        target = re.split(r"\s+(بخصوص|عن|كل|حتي|لين|في موضوع|حول)\s+", rest)[0].strip()
        target = re.sub(r"^(شركه|شركة|العميل|مؤسسه|مؤسسة)\s+", "", target)
        return Intent("follow_up", {"target": target, "every_hours": hours, "subject": subj.group(1) if subj else None,
                                    "raw": text})

    if re.match(r"^" + ASSIGN_VERBS, t) or re.search(r"(كلف|اطلب من)\s", t):
        return Intent("assign_task", {"raw": text, "priority": detect_priority(t),
                                      "requires_proof": bool(re.search(PROOF_WORDS, t))})
    return Intent("unknown", confident=False)


HELP_TEXT = """أهلًا 🌷 أنا نوف. أمثلة لما يمكنك قوله:
• أرسل لفهد تجهيز تقرير المبيعات اليوم قبل الساعة 2
• كلف سارة بمراجعة العقد بكرة الساعة 11 عاجل مع إثبات
• تابع مع هيفاء كل 12 ساعة حتى تنهي الموضوع
• تابع مع شركة الأفق بخصوص عرض السعر
• أضف موعدًا مع العميل خالد يوم الثلاثاء الساعة 11
• من لديه مهام متأخرة؟
• ماذا يحتاج قراري اليوم؟
• أرسل تقرير الموظفين اليومي / التقرير الصباحي / المسائي
• ما هي أهم العملاء والفرص التي لم نتابعها؟
• حالة المهمة T-12 — إلغاء المهمة T-12
• موافق A-5 — ارفض A-5"""
