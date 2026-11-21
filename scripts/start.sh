#!/bin/sh
# تشغيل خدمة الويب: ترحيل قاعدة البيانات، ثم (في بيئة التجربة فقط) تهيئة البيانات التجريبية، ثم الخادم.
set -e
alembic upgrade head
python -m scripts.bootstrap_demo
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
