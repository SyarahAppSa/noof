"""طبقة التقويم: Google Calendar أو Outlook (Microsoft Graph) أو داخلي فقط."""
import logging
import time

import httpx

from app.config import get_settings

log = logging.getLogger("noof.calendar")


class CalendarProvider:
    name = "none"

    def upsert_event(self, *, event_id: str | None, title: str, start, end, description: str = "",
                     location: str | None = None) -> str | None:
        return None

    def delete_event(self, event_id: str) -> None:
        return None


class GoogleCalendar(CalendarProvider):
    """OAuth2 Refresh Token لحساب المالك (نطاق calendar.events)."""

    name = "google"
    _token: tuple[str, float] | None = None

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        s = get_settings()
        r = httpx.post("https://oauth2.googleapis.com/token", timeout=20, data={
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "refresh_token": s.google_refresh_token, "grant_type": "refresh_token"})
        r.raise_for_status()
        d = r.json()
        GoogleCalendar._token = (d["access_token"], time.time() + d.get("expires_in", 3600))
        return d["access_token"]

    def upsert_event(self, *, event_id, title, start, end, description="", location=None):
        s = get_settings()
        body = {"summary": title, "description": description, "location": location or "",
                "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Riyadh"},
                "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Riyadh"}}
        base = f"https://www.googleapis.com/calendar/v3/calendars/{s.google_calendar_id}/events"
        h = {"Authorization": f"Bearer {self._access_token()}"}
        r = (httpx.patch(f"{base}/{event_id}", json=body, headers=h, timeout=20) if event_id
             else httpx.post(base, json=body, headers=h, timeout=20))
        r.raise_for_status()
        return r.json()["id"]

    def delete_event(self, event_id):
        s = get_settings()
        httpx.delete(f"https://www.googleapis.com/calendar/v3/calendars/{s.google_calendar_id}/events/{event_id}",
                     headers={"Authorization": f"Bearer {self._access_token()}"}, timeout=20)


class OutlookCalendar(CalendarProvider):
    """Microsoft Graph بتطبيق Azure AD (client credentials) وصلاحية Calendars.ReadWrite."""

    name = "outlook"
    _token: tuple[str, float] | None = None

    def _access_token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        s = get_settings()
        r = httpx.post(f"https://login.microsoftonline.com/{s.ms_tenant_id}/oauth2/v2.0/token", timeout=20, data={
            "client_id": s.ms_client_id, "client_secret": s.ms_client_secret,
            "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
        r.raise_for_status()
        d = r.json()
        OutlookCalendar._token = (d["access_token"], time.time() + d.get("expires_in", 3600))
        return d["access_token"]

    def upsert_event(self, *, event_id, title, start, end, description="", location=None):
        s = get_settings()
        body = {"subject": title, "body": {"contentType": "text", "content": description},
                "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Arab Standard Time"},
                "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Arab Standard Time"},
                "location": {"displayName": location or ""}}
        base = f"https://graph.microsoft.com/v1.0/users/{s.ms_user_id}/events"
        h = {"Authorization": f"Bearer {self._access_token()}"}
        r = (httpx.patch(f"{base}/{event_id}", json=body, headers=h, timeout=20) if event_id
             else httpx.post(base, json=body, headers=h, timeout=20))
        r.raise_for_status()
        return r.json()["id"]

    def delete_event(self, event_id):
        s = get_settings()
        httpx.delete(f"https://graph.microsoft.com/v1.0/users/{s.ms_user_id}/events/{event_id}",
                     headers={"Authorization": f"Bearer {self._access_token()}"}, timeout=20)


def get_calendar() -> CalendarProvider:
    p = get_settings().calendar_provider
    if p == "google":
        return GoogleCalendar()
    if p == "outlook":
        return OutlookCalendar()
    return CalendarProvider()
