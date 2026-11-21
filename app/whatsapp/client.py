"""عميل WhatsApp Cloud API الرسمي (Graph API) — بدون أي ربط غير رسمي."""
import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger("noof.wa")


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


# أخطاء Meta التي تعني أن نافذة 24 ساعة مغلقة ويلزم قالب معتمد
REENGAGEMENT_CODES = {131047, 131026}
RETRYABLE_CODES = {130429, 131048, 131056, 131000, 133004}


@dataclass
class SendResult:
    wa_message_id: str | None
    dry_run: bool = False


class WhatsAppClient:
    def __init__(self, phone_number_id: str | None = None):
        s = get_settings()
        self.s = s
        self.phone_number_id = phone_number_id or s.wa_phone_number_id
        self.base = f"{s.wa_graph_base}/{s.wa_api_version}"

    @property
    def dry_run(self) -> bool:
        return self.s.wa_dry_run or not (self.s.wa_access_token and self.phone_number_id)

    def _headers(self):
        return {"Authorization": f"Bearer {self.s.wa_access_token}"}

    def _post(self, payload: dict) -> SendResult:
        if self.dry_run:
            log.info("[WA DRY-RUN] %s", payload)
            return SendResult(wa_message_id=None, dry_run=True)
        url = f"{self.base}/{self.phone_number_id}/messages"
        try:
            r = httpx.post(url, json=payload, headers=self._headers(), timeout=20)
        except httpx.HTTPError as e:
            raise WhatsAppError(f"network: {e}", retryable=True) from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            code = err.get("code")
            raise WhatsAppError(
                err.get("message", r.text[:300]), code=code, status=r.status_code,
                retryable=r.status_code >= 500 or r.status_code == 429 or code in RETRYABLE_CODES,
            )
        data = r.json()
        mid = (data.get("messages") or [{}])[0].get("id")
        return SendResult(wa_message_id=mid)

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
                   "text": {"preview_url": False, "body": body[:4096]}}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return self._post(payload)

    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> SendResult:
        """أزرار رد سريع (حتى 3 أزرار، عنوان الزر حتى 20 حرفًا)."""
        payload = {
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {
                "type": "button", "body": {"text": body[:1024]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                                       for bid, title in buttons[:3]]},
            },
        }
        return self._post(payload)

    def send_template(self, to: str, name: str, params: list[str], lang: str | None = None,
                      button_payloads: list[str] | None = None) -> SendResult:
        components = []
        if params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": p[:1000]} for p in params]})
        for i, pl in enumerate(button_payloads or []):
            components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                               "parameters": [{"type": "payload", "payload": pl}]})
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                   "template": {"name": name, "language": {"code": lang or self.s.wa_template_lang},
                                "components": components}}
        return self._post(payload)

    def mark_read(self, wa_message_id: str) -> None:
        if self.dry_run:
            return
        try:
            httpx.post(f"{self.base}/{self.phone_number_id}/messages", headers=self._headers(), timeout=10,
                       json={"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id})
        except httpx.HTTPError:
            pass

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        """يحمّل الوسائط (صوت/صورة/مستند) عبر رابط مؤقت من Graph API."""
        if self.dry_run:
            raise WhatsAppError("dry-run: لا يمكن تحميل الوسائط")
        meta = httpx.get(f"{self.base}/{media_id}", headers=self._headers(), timeout=20)
        meta.raise_for_status()
        info = meta.json()
        r = httpx.get(info["url"], headers=self._headers(), timeout=60)
        r.raise_for_status()
        return r.content, info.get("mime_type", "application/octet-stream")


_override: WhatsAppClient | None = None


def get_client(phone_number_id: str | None = None) -> WhatsAppClient:
    if _override is not None:
        return _override
    return WhatsAppClient(phone_number_id)


def set_client_override(c):
    """للاختبارات: استبدال العميل بعميل وهمي."""
    global _override
    _override = c
