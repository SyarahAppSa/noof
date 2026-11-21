"""طبقة CRM مرنة: قاعدة بيانات نوف هي المصدر، ويُزامَن أي CRM خارجي عبر محوّل (Adapter).
لتغيير الـCRM مستقبلًا يكفي كتابة محوّل جديد بنفس الواجهة دون المساس ببقية النظام."""
import hashlib
import hmac
import json
import logging

import httpx

from app.config import get_settings

log = logging.getLogger("noof.crm")


class CRMAdapter:
    name = "internal"

    def upsert_client(self, data: dict) -> str | None:
        return None

    def upsert_opportunity(self, data: dict) -> str | None:
        return None

    def log_activity(self, data: dict) -> None:
        return None


class WebhookCRM(CRMAdapter):
    """يرسل الأحداث كـ JSON موقّع (HMAC-SHA256) لأي نظام — مناسب لـ Zapier/Make/n8n أو CRM داخلي."""

    name = "webhook"

    def _post(self, event: str, data: dict) -> dict:
        s = get_settings()
        body = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str).encode()
        sig = hmac.new(s.crm_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        r = httpx.post(s.crm_webhook_url, content=body, timeout=20,
                       headers={"Content-Type": "application/json", "X-Noof-Signature": f"sha256={sig}"})
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {}

    def upsert_client(self, data):
        return self._post("client.upsert", data).get("id")

    def upsert_opportunity(self, data):
        return self._post("opportunity.upsert", data).get("id")

    def log_activity(self, data):
        self._post("activity", data)


class HubSpotCRM(CRMAdapter):
    name = "hubspot"
    base = "https://api.hubapi.com/crm/v3/objects"

    def _h(self):
        return {"Authorization": f"Bearer {get_settings().hubspot_token}"}

    def upsert_client(self, data):
        props = {"name": data.get("company") or data["name"], "phone": data.get("phone") or "",
                 "industry": data.get("sector") or ""}
        ext = data.get("crm_external_id")
        r = (httpx.patch(f"{self.base}/companies/{ext}", json={"properties": props}, headers=self._h(), timeout=20)
             if ext else httpx.post(f"{self.base}/companies", json={"properties": props}, headers=self._h(), timeout=20))
        r.raise_for_status()
        return r.json()["id"]

    def upsert_opportunity(self, data):
        props = {"dealname": data["title"], "amount": str(data.get("estimated_value") or "")}
        ext = data.get("crm_external_id")
        r = (httpx.patch(f"{self.base}/deals/{ext}", json={"properties": props}, headers=self._h(), timeout=20)
             if ext else httpx.post(f"{self.base}/deals", json={"properties": props}, headers=self._h(), timeout=20))
        r.raise_for_status()
        return r.json()["id"]


def get_crm() -> CRMAdapter:
    p = get_settings().crm_provider
    if p == "webhook" and get_settings().crm_webhook_url:
        return WebhookCRM()
    if p == "hubspot" and get_settings().hubspot_token:
        return HubSpotCRM()
    return CRMAdapter()
