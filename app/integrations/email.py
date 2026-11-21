"""إرسال البريد (SMTP) — للتقارير حاليًا، وجاهز للتوسعة لاحقًا لاستقبال البريد."""
import smtplib
from email.message import EmailMessage

from app.config import get_settings


def send_email(to: str, subject: str, body: str) -> bool:
    s = get_settings()
    if not (s.email_enabled and s.smtp_host and to):
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return True
