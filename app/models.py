"""نموذج البيانات الكامل لنوف."""
import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.security import EncryptedText
from app.timeutil import utcnow


def _enum(e, name):
    return Enum(e, name=name, values_callable=lambda x: [i.value for i in x], native_enum=False, length=32)


class Role(str, enum.Enum):
    owner = "owner"
    manager = "manager"
    employee = "employee"


class TaskStatus(str, enum.Enum):
    new = "new"  # جديدة
    acknowledged = "acknowledged"  # تم الاستلام
    in_progress = "in_progress"  # تحت التنفيذ
    pending_review = "pending_review"  # بانتظار مراجعة
    completed = "completed"  # مكتملة
    overdue = "overdue"  # متأخرة
    cancelled = "cancelled"  # ملغاة


OPEN_TASK_STATUSES = (
    TaskStatus.new,
    TaskStatus.acknowledged,
    TaskStatus.in_progress,
    TaskStatus.overdue,
    TaskStatus.pending_review,
)


class Priority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class MessageCategory(str, enum.Enum):
    client = "client"  # عميل
    employee = "employee"  # موظف
    supplier = "supplier"  # مورد
    urgent = "urgent"  # طلب عاجل
    complaint = "complaint"  # شكوى
    appointment = "appointment"  # موعد
    sales_opportunity = "sales_opportunity"  # فرصة مبيعات
    follow_up = "follow_up"  # متابعة
    personal = "personal"  # رسالة شخصية
    command = "command"  # أمر من المالك/المدير
    other = "other"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class ApprovalKind(str, enum.Enum):
    send_message = "send_message"  # إرسال رد/رسالة لطرف خارجي
    financial = "financial"
    final_price = "final_price"
    contract = "contract"
    delete_data = "delete_data"
    commitment = "commitment"
    broadcast = "broadcast"
    appointment_confirmation = "appointment_confirmation"
    task_review = "task_review"
    deadline_extension = "deadline_extension"


class OppStage(str, enum.Enum):
    new = "new"
    contacted = "contacted"
    quotation = "quotation"  # عرض سعر
    negotiation = "negotiation"
    contract = "contract"
    won = "won"
    lost = "lost"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"  # سيعاد المحاولة
    dead = "dead"  # استنفد المحاولات


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class User(TimestampMixin, Base):
    """فريق العمل: المالك، المدراء، الموظفون."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    aliases: Mapped[list] = mapped_column(JSON, default=list)  # أسماء مختصرة للتعرف عليه في الأوامر
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), default=Role.employee)
    title: Mapped[str | None] = mapped_column(String(120))
    department: Mapped[str | None] = mapped_column(String(120))
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str | None] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    manager = relationship("User", remote_side=[id])


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user = relationship("User")


class Client(TimestampMixin, Base):
    """ملف العميل/الشركة."""

    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    company: Mapped[str | None] = mapped_column(String(200))
    sector: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="lead")  # lead | active | inactive | vip
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # الموظف المسؤول
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(EncryptedText)
    crm_external_id: Mapped[str | None] = mapped_column(String(120))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    owner = relationship("User")


class Contact(TimestampMixin, Base):
    """أي رقم واتساب خارجي (عميل، مورد، شخص)."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), default="unknown")  # client | supplier | personal | unknown
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    client = relationship("Client")


class Message(TimestampMixin, Base):
    """كل رسالة واردة وصادرة — المحتوى مشفّر."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    direction: Mapped[str] = mapped_column(String(8))  # in | out | echo
    channel_phone_number_id: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", use_alter=True, name="fk_messages_task_id"))
    msg_type: Mapped[str] = mapped_column(String(32), default="text")
    body: Mapped[str | None] = mapped_column(EncryptedText)
    media_id: Mapped[str | None] = mapped_column(String(128))
    media_mime: Mapped[str | None] = mapped_column(String(100))
    media_filename: Mapped[str | None] = mapped_column(String(255))
    extracted_text: Mapped[str | None] = mapped_column(EncryptedText)  # نص الصوت/الصورة/المستند
    summary: Mapped[str | None] = mapped_column(EncryptedText)
    context_wa_id: Mapped[str | None] = mapped_column(String(128))  # الرسالة التي يُرد عليها
    interactive_payload: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(32))
    urgency: Mapped[str | None] = mapped_column(String(16))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    needs_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str | None] = mapped_column(String(32))  # sent | delivered | read | failed | dry_run
    error: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(String(120))
    raw: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("ix_messages_phone_created", "phone", "created_at"),)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(EncryptedText)
    assignee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    priority: Mapped[Priority] = mapped_column(_enum(Priority, "priority"), default=Priority.normal)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus, "task_status"), default=TaskStatus.new, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"))
    related_party: Mapped[str | None] = mapped_column(String(200))  # الجهة المرتبطة
    requires_proof: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_interval_hours: Mapped[float] = mapped_column(Numeric(6, 2), default=12)
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int] = mapped_column(Integer, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overdue_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_note: Mapped[str | None] = mapped_column(EncryptedText)
    assignment_wa_id: Mapped[str | None] = mapped_column(String(128))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    assignee = relationship("User", foreign_keys=[assignee_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    client = relationship("Client")
    events = relationship("TaskEvent", order_by="TaskEvent.id", back_populates="task")
    attachments = relationship("TaskAttachment", back_populates="task")

    @property
    def code(self) -> str:
        return f"T-{self.id}"


class TaskEvent(Base):
    """سجل المتابعة الكامل لكل مهمة."""

    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    task = relationship("Task", back_populates="events")
    actor = relationship("User")


class TaskAttachment(Base):
    __tablename__ = "task_attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    media_id: Mapped[str | None] = mapped_column(String(128))
    mime: Mapped[str | None] = mapped_column(String(100))
    filename: Mapped[str | None] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(EncryptedText)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    task = relationship("Task", back_populates="attachments")


class Opportunity(TimestampMixin, Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    stage: Mapped[OppStage] = mapped_column(_enum(OppStage, "opp_stage"), default=OppStage.new)
    estimated_value: Mapped[float | None] = mapped_column(Numeric(14, 2))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    quote_status: Mapped[str | None] = mapped_column(String(32))  # requested | draft | sent | accepted | rejected
    contract_status: Mapped[str | None] = mapped_column(String(32))  # draft | sent | signed
    collection_status: Mapped[str | None] = mapped_column(String(32))  # pending | partial | collected
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", use_alter=True, name="fk_opportunities_source_message_id"))
    notes: Mapped[str | None] = mapped_column(EncryptedText)
    crm_external_id: Mapped[str | None] = mapped_column(String(120))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    client = relationship("Client")
    owner = relationship("User")


class Appointment(TimestampMixin, Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    person_name: Mapped[str | None] = mapped_column(String(200))
    person_phone: Mapped[str | None] = mapped_column(String(20))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(32), default="tentative")  # tentative | confirmed | cancelled
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    calendar_provider: Mapped[str | None] = mapped_column(String(32))
    external_event_id: Mapped[str | None] = mapped_column(String(255))
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(EncryptedText)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    client = relationship("Client")


class Approval(TimestampMixin, Base):
    """أي إجراء حساس ينتظر موافقة المالك الصريحة."""

    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ApprovalKind] = mapped_column(_enum(ApprovalKind, "approval_kind"))
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.pending, index=True
    )
    summary: Mapped[str] = mapped_column(EncryptedText)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(EncryptedText)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"))
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def code(self) -> str:
        return f"A-{self.id}"


class StaffRequest(TimestampMixin, Base):
    """طلبات الموظفين المفتوحة (إجازة، عهدة، استفسار...)."""

    __tablename__ = "staff_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(EncryptedText)
    category: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    user = relationship("User")


class ConversationState(TimestampMixin, Base):
    """حالة حوار متعدد الخطوات (مثل: بانتظار إثبات إنجاز)."""

    __tablename__ = "conversation_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutoReplyRule(TimestampMixin, Base):
    """ردود معتمدة فقط — نوف لا ترد تلقائيًا إلا من هذه القائمة."""

    __tablename__ = "auto_reply_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), onupdate=utcnow)


class Job(Base):
    """طابور مهام خلفي داخل PostgreSQL (FOR UPDATE SKIP LOCKED) — لا تضيع المهام عند الانقطاع."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus, "job_status"), default=JobStatus.queued)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_ready", "status", "run_at"),)


class AuditLog(Base):
    """سجل تدقيق غير قابل للتعديل أو الحذف (محمي بـ trigger في قاعدة البيانات)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(16))  # user | system | contact
    actor_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ReportLog(Base):
    __tablename__ = "report_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    period_key: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(EncryptedText)
    sent_to: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    __table_args__ = (UniqueConstraint("kind", "period_key", name="uq_report_period"),)
