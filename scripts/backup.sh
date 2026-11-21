#!/bin/sh
# نسخة احتياطية يومية مضغوطة لقاعدة البيانات مع حذف النسخ الأقدم من RETENTION_DAYS.
# الاستعادة: gunzip -c backups/noof_YYYYMMDD_HHMM.sql.gz | psql -h db -U noof noof
set -e
TS=$(date +%Y%m%d_%H%M)
pg_dump -h "${PGHOST:-db}" -U "${PGUSER:-noof}" "${PGDATABASE:-noof}" | gzip > "/backups/noof_${TS}.sql.gz"
find /backups -name 'noof_*.sql.gz' -mtime +"${RETENTION_DAYS:-14}" -delete
echo "backup ok: noof_${TS}.sql.gz"
