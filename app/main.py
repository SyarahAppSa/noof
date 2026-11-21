"""تطبيق نوف — FastAPI."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.api import dashboard, rest, webhook
from app.config import get_settings
from app.db import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
s = get_settings()
if s.app_env == "production" and (s.secret_key == "change-me" or not s.encryption_key or not s.wa_app_secret):
    raise RuntimeError("في الإنتاج يجب ضبط SECRET_KEY و ENCRYPTION_KEY و WA_APP_SECRET")

@asynccontextmanager
async def lifespan(_app):
    if s.embedded_worker:
        from app.worker import start_embedded
        start_embedded()
    yield


app = FastAPI(lifespan=lifespan, title="نوف — مركز قيادة الشركة عبر واتساب", version="1.0.0",
              docs_url=None if s.app_env == "production" else "/docs", redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=s.secret_key, session_cookie="noof_session", max_age=60 * 60 * 12,
                   same_site="lax", https_only=s.app_env == "production")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if s.app_env == "production":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


@app.exception_handler(dashboard.NeedLogin)
async def need_login(request: Request, exc):
    return RedirectResponse("/dashboard/login", 303)


app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(webhook.router)
app.include_router(rest.router)
app.include_router(dashboard.router)


@app.get("/")
def root():
    return RedirectResponse("/dashboard")


@app.get("/healthz")
def health():
    with get_engine().connect() as c:
        c.execute(text("SELECT 1"))
    return {"status": "ok", "whatsapp_dry_run": s.wa_dry_run}
