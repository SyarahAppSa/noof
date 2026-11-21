"""محاكي واتساب في لوحة التحكم (وضع التجربة)."""
import re

from sqlalchemy import select

from app.models import Task


def _login(http):
    csrf = re.search(r'name="csrf" value="([^"]+)"', http.get("/dashboard/login").text).group(1)
    http.post("/dashboard/login", data={"username": "owner", "password": "owner-pass", "csrf": csrf})
    return csrf


def test_simulator_disabled_by_default(team, http):
    _login(http)
    assert http.get("/dashboard/simulator").status_code == 404


def test_simulator_runs_full_flow(team, http, db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "simulator_enabled", True)
    monkeypatch.setattr(get_settings(), "wa_dry_run", True)
    _login(http)
    csrf = re.search(r'name="csrf" value="([^"]+)"', http.get("/dashboard/simulator").text).group(1)
    r = http.post("/dashboard/simulator", data={"phone": "966500000001", "text": "أرسل لفهد تجهيز العرض", "csrf": csrf})
    assert r.status_code == 200 and "أنشأت المهمة" in r.text
    t = db.scalars(select(Task)).first()
    r = http.post("/dashboard/simulator", data={"phone": "966500000003", "text": f"btn:task:ack:{t.id}", "csrf": csrf})
    assert "تم تسجيل استلام" in r.text
