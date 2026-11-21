"""تحويل الرسائل الصوتية العربية إلى نص — مزوّد قابل للتبديل.

azure : Azure AI Speech (يدعم ar-SA اللهجة السعودية) — REST للمقاطع القصيرة حتى 60 ثانية.
openai: OpenAI Whisper API.
none  : معطّل؛ تُحوَّل الرسالة الصوتية للمالك كما هي مع تنبيه.
"""
import logging

import httpx

from app.config import get_settings

log = logging.getLogger("noof.stt")


def transcribe(audio: bytes, mime: str) -> str | None:
    s = get_settings()
    try:
        if s.stt_provider == "azure" and s.azure_speech_key and s.azure_speech_region:
            url = (f"https://{s.azure_speech_region}.stt.speech.microsoft.com/speech/recognition/conversation/"
                   f"cognitiveservices/v1?language={s.stt_language}&format=simple")
            ctype = "audio/ogg; codecs=opus" if "ogg" in mime else mime
            r = httpx.post(url, content=audio, timeout=60,
                           headers={"Ocp-Apim-Subscription-Key": s.azure_speech_key, "Content-Type": ctype})
            r.raise_for_status()
            data = r.json()
            return data.get("DisplayText") if data.get("RecognitionStatus") == "Success" else None
        if s.stt_provider == "openai" and s.openai_api_key:
            ext = "ogg" if "ogg" in mime else (mime.split("/")[-1] or "ogg")
            r = httpx.post("https://api.openai.com/v1/audio/transcriptions", timeout=120,
                           headers={"Authorization": f"Bearer {s.openai_api_key}"},
                           data={"model": s.openai_stt_model, "language": "ar"},
                           files={"file": (f"voice.{ext}", audio, mime)})
            r.raise_for_status()
            return r.json().get("text")
    except httpx.HTTPError as e:
        log.error("STT failed: %s", e)
    return None
