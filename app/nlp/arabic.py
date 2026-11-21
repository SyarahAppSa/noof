import re

_TASHKEEL = re.compile(r"[ؗ-ًؚ-ْـ]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def to_western_digits(text: str) -> str:
    return text.translate(_AR_DIGITS)


def normalize(text: str) -> str:
    """توحيد الكتابة العربية للمطابقة: إزالة التشكيل والتطويل وتوحيد الألف والياء."""
    if not text:
        return ""
    t = to_western_digits(text)
    t = _TASHKEEL.sub("", t)
    t = re.sub("[إأآٱ]", "ا", t)
    t = t.replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    t = t.replace("،", ",").replace("؟", "?")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def norm_name(text: str) -> str:
    t = normalize(text).replace("ة", "ه")
    return t.lower()


def normalize_phone(raw: str, default_cc: str = "966") -> str:
    """يحوّل الرقم لصيغة wa_id: أرقام فقط بالمفتاح الدولي، مثل 9665XXXXXXXX."""
    digits = re.sub(r"\D", "", to_western_digits(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("05") and len(digits) == 10:
        digits = default_cc + digits[1:]
    elif digits.startswith("5") and len(digits) == 9:
        digits = default_cc + digits
    return digits
