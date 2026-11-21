"""تحليل التواريخ والأوقات من العربية (الفصحى واللهجة السعودية) بتوقيت الرياض.

أمثلة مدعومة:
  اليوم قبل الساعة 2 | بكرة الساعة 10 الصبح | يوم الثلاثاء الساعة 11 | بعد ساعتين
  خلال 3 أيام | 25/11 الساعة 4 العصر | الساعة 9 ونص الليل | قبل نهاية الدوام
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.config import TZ
from app.nlp.arabic import normalize
from app.timeutil import riyadh_now

WEEKDAYS = {
    "الاثنين": 0, "الاتنين": 0, "اثنين": 0,
    "الثلاثاء": 1, "الثلاثا": 1, "ثلاثاء": 1,
    "الاربعاء": 2, "الاربعا": 2, "اربعاء": 2,
    "الخميس": 3, "خميس": 3,
    "الجمعه": 4, "الجمعة": 4, "جمعه": 4,
    "السبت": 5, "سبت": 5,
    "الاحد": 6, "احد": 6,
}

WORD_NUMS = {
    "واحد": 1, "وحده": 1, "واحده": 1, "اثنين": 2, "ثنتين": 2, "ثلاث": 3, "ثلاثه": 3, "اربع": 4, "اربعه": 4,
    "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6, "سبع": 7, "سبعه": 7, "ثمان": 8, "ثمانيه": 8, "تسع": 9,
    "تسعه": 9, "عشر": 10, "عشره": 10, "احدعش": 11, "اطنعش": 12,
}

PM_WORDS = r"(مساء|مساءا|مساءً|المساء|العصر|عصرا|الظهر|ظهرا|المغرب|الليل|بالليل|ليلا|م)"
AM_WORDS = r"(صباحا|صباحاً|الصبح|الصباح|صباح|الفجر|ص)"


@dataclass
class ParsedWhen:
    dt: datetime | None = None
    has_date: bool = False
    has_time: bool = False
    is_deadline: bool = False
    spans: list[tuple[int, int]] = field(default_factory=list)


def parse_when(text: str, *, now: datetime | None = None, work_end: str = "17:00",
               default_hour_for_date: int | None = None) -> ParsedWhen:
    """يرجع datetime بتوقيت الرياض (aware) أو None."""
    now = (now or riyadh_now()).astimezone(TZ)
    t = normalize(text)
    res = ParsedWhen()
    res.is_deadline = bool(re.search(r"(قبل|بحد اقصي|اخر موعد|موعد نهائي|خلال)", t))

    # 1) فترات نسبية: بعد/خلال ساعتين، 3 ساعات، يومين، أسبوع
    rel = re.search(
        r"(بعد|خلال|في خلال)\s+(?:(\d+(?:\.\d+)?)\s*)?(ساعتين|ساعه|ساعات|ساعة|دقيقه|دقائق|دقيقة|يومين|يوم|ايام|اسبوعين|اسبوع)",
        t,
    )
    if rel:
        num, unit = rel.group(2), rel.group(3)
        if unit in ("ساعتين", "يومين", "اسبوعين"):
            n = float(num) if num else 2
        else:
            n = float(num) if num else 1
        if unit.startswith("ساع"):
            delta = timedelta(hours=n)
        elif unit.startswith("دقي"):
            delta = timedelta(minutes=n)
        elif unit.startswith("اسبوع"):
            delta = timedelta(weeks=n)
        else:
            delta = timedelta(days=n)
        res.dt = now + delta
        res.has_date = res.has_time = True
        res.spans.append(rel.span())
        if unit.startswith(("يوم", "ايام", "اسبوع")):
            # "خلال 3 أيام" = نهاية الدوام في ذلك اليوم
            h, m = map(int, work_end.split(":"))
            res.dt = res.dt.replace(hour=h, minute=m, second=0, microsecond=0)
        return res

    # 2) اليوم المقصود
    day: datetime | None = None
    weekday_match = False
    m = re.search(r"(اليوم|الليله|بعد بكره|بعد بكرة|بعد غد|بكره|بكرة|بكرا|غدا|غدًا|غداً|الغد)", t)
    if m:
        w = m.group(1)
        offset = 2 if w.startswith("بعد") else (0 if w in ("اليوم", "الليله") else 1)
        day = now + timedelta(days=offset)
        res.spans.append(m.span())
    else:
        m = re.search(r"(?:يوم\s+)?(?:(?:ال)?(?:جاي|قادم|القادم)\s+)?(" + "|".join(WEEKDAYS) + r")(?:\s+(?:الجاي|القادم|الجاي))?", t)
        if m:
            target = WEEKDAYS[m.group(1)]
            diff = (target - now.weekday()) % 7
            day = now + timedelta(days=diff)
            weekday_match = True
            res.spans.append(m.span())
        else:
            m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b", t)
            if m:
                d, mo = int(m.group(1)), int(m.group(2))
                y = int(m.group(3)) if m.group(3) else now.year
                if y < 100:
                    y += 2000
                try:
                    day = now.replace(year=y, month=mo, day=d)
                    if not m.group(3) and day.date() < now.date():
                        day = day.replace(year=y + 1)
                    res.spans.append(m.span())
                except ValueError:
                    day = None
    if day is not None:
        res.has_date = True

    # 3) الوقت
    hour = minute = None
    tm = re.search(
        r"(?:الساعه|الساعة|ساعه|الساعة)\s*(\d{1,2})(?::(\d{2}))?\s*(ونص|و نص|وربع|و ربع|الا ربع|إلا ربع)?\s*" + f"(?:{PM_WORDS}|{AM_WORDS})?",
        t,
    )
    if not tm:
        tm = re.search(r"\b(\d{1,2}):(\d{2})\s*()" + f"(?:{PM_WORDS}|{AM_WORDS})?", t)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2)) if tm.group(2) else 0
        frac = (tm.group(3) or "").replace(" ", "")
        if frac == "ونص":
            minute = 30
        elif frac == "وربع":
            minute = 15
        elif frac == "الاربع":
            hour -= 1
            minute = 45
        pm = tm.group(4)
        am = tm.group(5)
        if pm and hour < 12:
            hour += 12
        elif am and hour == 12:
            hour = 0
        elif not pm and not am:
            # بدون تحديد: ساعات العمل — 1 إلى 7 تعني بعد الظهر
            if 1 <= hour <= 7:
                hour += 12
        res.spans.append(tm.span())
    elif re.search(r"(نهايه الدوام|نهاية الدوام|اخر الدوام|آخر الدوام)", t):
        hour, minute = map(int, work_end.split(":"))
        res.spans.append(re.search(r"(نهايه الدوام|نهاية الدوام|اخر الدوام|آخر الدوام)", t).span())
    elif re.search(r"\b(الظهر)\b", t):
        hour, minute = 12, 0
    elif re.search(r"\b(العصر)\b", t):
        hour, minute = 15, 30

    if hour is not None and 0 <= hour <= 23:
        res.has_time = True
        base = day or now
        candidate = base.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        if day is None and candidate <= now:
            candidate += timedelta(days=1)  # "الساعة 2" وقد مضت => غدًا
        elif weekday_match and candidate <= now:
            candidate += timedelta(days=7)  # "يوم الثلاثاء" واليوم ثلاثاء ومضى الوقت => الأسبوع القادم
        res.dt = candidate
    elif day is not None:
        h, mi = (default_hour_for_date, 0) if default_hour_for_date is not None else map(int, work_end.split(":"))
        res.dt = day.replace(hour=h, minute=mi, second=0, microsecond=0)
    return res


def strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """يحذف عبارات الوقت من النص (المؤشرات على النص الموحّد)."""
    t = normalize(text)
    for s, e in sorted(spans, reverse=True):
        t = t[:s] + " " + t[e:]
    t = re.sub(r"\b(قبل|بحد اقصي|يوم|في|علي|الساعه)\s*$", "", t.strip())
    t = re.sub(r"\s+(قبل|يوم|في|الساعه)\s*(?=$|,|\.)", " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,.-")


def parse_time_of_day(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)
