"""إعدادات البنية التحتية والأسرار — تُقرأ من متغيرات البيئة فقط.

الإعدادات التشغيلية القابلة للتعديل من لوحة التحكم (أوقات التقارير، ساعات العمل،
فترات التذكير، أسلوب الرد) موجودة في جدول settings عبر app/services/settings.py.
"""
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TZ = ZoneInfo("Asia/Riyadh")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # عام
    app_env: str = "development"  # development | production | test
    app_base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"  # لتوقيع جلسات لوحة التحكم
    encryption_key: str = ""  # مفتاح Fernet لتشفير المحتوى الحساس
    database_url: str = "postgresql+psycopg://noof:noof@localhost:5432/noof"

    # المالك
    owner_name: str = "المالك"
    owner_phone: str = ""  # بصيغة دولية بدون + مثال 9665XXXXXXXX

    # WhatsApp Cloud API (الرسمي من Meta)
    wa_api_version: str = "v23.0"
    wa_graph_base: str = "https://graph.facebook.com"
    wa_access_token: str = ""  # System User token دائم
    wa_phone_number_id: str = ""  # رقم نوف (المساعدة) على Cloud API
    wa_owner_inbox_phone_number_id: str = ""  # اختياري: رقم المالك عبر Coexistence
    wa_business_account_id: str = ""
    wa_app_secret: str = ""  # للتحقق من توقيع X-Hub-Signature-256
    wa_verify_token: str = ""  # لمصافحة Webhook
    wa_dry_run: bool = True  # True = لا يرسل فعليًا، يسجّل فقط (للتطوير)
    wa_template_lang: str = "ar"

    # Claude (Anthropic)
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_enabled: bool = True

    # تحويل الصوت إلى نص
    stt_provider: str = "none"  # azure | openai | none
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    stt_language: str = "ar-SA"
    openai_api_key: str = ""
    openai_stt_model: str = "whisper-1"

    # التقويم
    calendar_provider: str = "none"  # google | outlook | none
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_user_id: str = ""  # بريد/معرّف صاحب التقويم في Outlook

    # CRM
    crm_provider: str = "internal"  # internal | webhook | hubspot
    crm_webhook_url: str = ""
    crm_webhook_secret: str = ""
    hubspot_token: str = ""

    # البريد
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    owner_email: str = ""

    # العامل الخلفي
    worker_poll_seconds: float = 2.0
    embedded_worker: bool = False  # تشغيل العامل داخل الويب (للتجربة/الخطط المجانية)
    simulator_enabled: bool = False  # صفحة محاكي واتساب في اللوحة (تعمل فقط مع WA_DRY_RUN)
    scheduler_tick_seconds: int = 60

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        """يقبل روابط Render/Heroku (postgres:// أو postgresql://) ويحوّلها لمشغّل psycopg3."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()
