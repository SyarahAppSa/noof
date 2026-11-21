"""طابور مهام موثوق داخل PostgreSQL.

- enqueue داخل نفس معاملة قاعدة البيانات (Transactional outbox) — لا تضيع الرسالة إذا تعطل الخادم.
- claim باستخدام FOR UPDATE SKIP LOCKED لدعم عدة عمّال بالتوازي.
- إعادة المحاولة بتأخير أسي، ثم حالة dead للمراجعة اليدوية.
- dedupe_key يمنع تكرار التذكير أو التقرير نفسه.
"""
import socket
import traceback
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from app.timeutil import utcnow

WORKER_ID = f"{socket.gethostname()}"
STALE_LOCK = timedelta(minutes=10)


def enqueue(db: Session, kind: str, payload: dict | None = None, *, run_at=None, dedupe_key: str | None = None,
            max_attempts: int = 6) -> Job | None:
    if dedupe_key:
        exists = db.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key))
        if exists:
            return None
    job = Job(kind=kind, payload=payload or {}, run_at=run_at or utcnow(), dedupe_key=dedupe_key,
              max_attempts=max_attempts, status=JobStatus.queued)
    if dedupe_key:
        try:
            with db.begin_nested():
                db.add(job)
                db.flush()
        except IntegrityError:
            return None
    else:
        db.add(job)
        db.flush()
    return job


def claim(db: Session, limit: int = 10) -> list[Job]:
    now = utcnow()
    # استرجاع مهام علقت بسبب انقطاع عامل سابق
    db.execute(
        text("UPDATE jobs SET status='queued', locked_at=NULL, locked_by=NULL "
             "WHERE status='running' AND locked_at < :stale"),
        {"stale": now - STALE_LOCK},
    )
    rows = db.scalars(
        select(Job)
        .where(Job.status.in_([JobStatus.queued, JobStatus.failed]), Job.run_at <= now)
        .order_by(Job.run_at, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for j in rows:
        j.status = JobStatus.running
        j.locked_at = now
        j.locked_by = WORKER_ID
        j.attempts += 1
    db.commit()
    return rows


def mark_done(db: Session, job: Job):
    job.status = JobStatus.done
    job.finished_at = utcnow()
    job.locked_at = None


def mark_failed(db: Session, job: Job, exc: BaseException):
    job.last_error = "".join(traceback.format_exception_only(type(exc), exc))[-2000:]
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.dead
        job.finished_at = utcnow()
    else:
        job.status = JobStatus.failed
        job.run_at = utcnow() + timedelta(seconds=min(30 * (2 ** (job.attempts - 1)), 3600))
