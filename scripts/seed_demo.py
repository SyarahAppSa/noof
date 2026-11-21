"""بيانات تجريبية فقط ⚠️ — كل السجلات موسومة is_demo=True وأسماؤها تبدأ بـ [تجريبي].
الأرقام وهمية (9665000000xx) ولن تصلها رسائل حقيقية.

python -m scripts.seed_demo          # إضافة
python -m scripts.seed_demo --purge  # حذف كل البيانات التجريبية
"""
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from app.db import session_scope
from app.models import (
    Appointment,
    AutoReplyRule,
    Client,
    Opportunity,
    Priority,
    Role,
    Task,
    TaskEvent,
    User,
)
from app.security import hash_password
from app.services import tasks as task_svc
from app.timeutil import utcnow

P = "[تجريبي] "


def seed():
    with session_scope() as db:
        if db.scalar(select(User).where(User.is_demo.is_(True))):
            print("البيانات التجريبية موجودة مسبقًا.")
            return
        owner = db.scalar(select(User).where(User.role == Role.owner))
        if owner is None:
            owner = User(name=P + "المالك", phone="966500000099", role=Role.owner, username="demo_owner",
                         password_hash=hash_password("demo-owner-2026"), is_demo=True)
            db.add(owner)
            db.flush()
        mgr = User(name=P + "سلطان الحربي", aliases=["سلطان"], phone="966500000010", role=Role.manager,
                   title="مدير المبيعات", is_demo=True, username="demo_manager",
                   password_hash=hash_password("demo-manager-2026"))
        db.add(mgr)
        db.flush()
        fahad = User(name=P + "فهد العتيبي", aliases=["فهد"], phone="966500000011", role=Role.employee,
                     manager_id=mgr.id, title="أخصائي مبيعات", is_demo=True)
        sara = User(name=P + "سارة القحطاني", aliases=["سارة"], phone="966500000012", role=Role.employee,
                    manager_id=mgr.id, title="محاسبة", is_demo=True)
        haifa = User(name=P + "هيفاء الشمري", aliases=["هيفاء", "هيفا"], phone="966500000013", role=Role.employee,
                     title="منسقة مشاريع", is_demo=True)
        db.add_all([fahad, sara, haifa])
        db.flush()
        c1 = Client(name=P + "شركة الأفق للتقنية", company="شركة الأفق للتقنية", sector="تقنية", phone="966500000021",
                    status="active", is_vip=True, owner_user_id=fahad.id, is_demo=True,
                    last_contact_at=utcnow() - timedelta(days=9))
        c2 = Client(name=P + "مؤسسة الندى التجارية", company="مؤسسة الندى", sector="تجزئة", phone="966500000022",
                    status="lead", owner_user_id=fahad.id, is_demo=True, last_contact_at=utcnow() - timedelta(days=2))
        db.add_all([c1, c2])
        db.flush()
        db.add(Opportunity(client_id=c1.id, title=P + "عقد صيانة سنوي", stage="quotation", estimated_value=85000,
                           quote_status="sent", owner_user_id=fahad.id, is_demo=True))
        db.add(Appointment(title=P + "اجتماع مراجعة عرض الأفق", client_id=c1.id, person_name="م. خالد",
                           start_at=utcnow() + timedelta(days=1, hours=2), end_at=utcnow() + timedelta(days=1, hours=3),
                           status="confirmed", is_demo=True))
        for title, who, pr, due in [(P + "تجهيز تقرير المبيعات الأسبوعي", fahad, Priority.high, timedelta(hours=5)),
                                    (P + "مراجعة فواتير المورد", sara, Priority.normal, timedelta(days=2)),
                                    (P + "تحديث خطة المشروع", haifa, Priority.urgent, timedelta(hours=-3))]:
            t = task_svc.create_task(db, title=title, assignee=who, created_by=owner, priority=pr,
                                     due_at=utcnow() + due, notify=False)
            t.is_demo = True
        db.add(AutoReplyRule(name=P + "أوقات الدوام", keywords=["الدوام", "اوقات العمل", "متى تفتحون"],
                             answer="أهلًا بكم 🌷 أوقات العمل من الأحد إلى الخميس، من 8 صباحًا حتى 5 مساءً.",
                             enabled=True, approved_by_id=owner.id))
    print("✅ أُضيفت بيانات تجريبية (موسومة [تجريبي]). دخول تجريبي: demo_manager / demo-manager-2026")


def purge():
    with session_scope() as db:
        demo_tasks = select(Task.id).where(Task.is_demo.is_(True))
        db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(demo_tasks)))
        db.execute(delete(Task).where(Task.is_demo.is_(True)))
        db.execute(delete(Appointment).where(Appointment.is_demo.is_(True)))
        db.execute(delete(Opportunity).where(Opportunity.is_demo.is_(True)))
        db.execute(delete(Client).where(Client.is_demo.is_(True)))
        db.execute(delete(AutoReplyRule).where(AutoReplyRule.name.startswith(P)))
        db.execute(delete(User).where(User.is_demo.is_(True), User.role != Role.owner))
        db.execute(delete(User).where(User.is_demo.is_(True)))
    print("🧹 حُذفت البيانات التجريبية.")


if __name__ == "__main__":
    purge() if "--purge" in sys.argv else seed()
