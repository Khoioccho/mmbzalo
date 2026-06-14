from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, UUID, BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MembershipRole(str, enum.Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class AuthSessionState(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class WorkspaceLoginState(str, enum.Enum):
    IDLE = "idle"
    WAITING_QR = "waiting_qr"
    AUTHENTICATED = "authenticated"
    EXPIRED = "expired"
    ERROR = "error"


class WorkerState(str, enum.Enum):
    ONLINE = "online"
    DRAINING = "draining"
    OFFLINE = "offline"


class JobType(str, enum.Enum):
    CONTACT_SYNC = "contact_sync"
    MANUAL_SEND = "manual_send"
    CAMPAIGN_SEND = "campaign_send"
    GROUP_SEND = "group_send"
    FRIEND_REQUEST = "friend_request"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobFailureClass(str, enum.Enum):
    NONE = "none"
    VALIDATION = "validation"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    SESSION_EXPIRED = "session_expired"


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AuditActorType(str, enum.Enum):
    USER = "user"
    WORKER = "worker"
    SYSTEM = "system"


def _json_type():
    try:
        return JSONB
    except Exception:
        return JSON


JSONType = _json_type()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    active_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL")
    )
    session_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    state: Mapped[AuthSessionState] = mapped_column(Enum(AuthSessionState), default=AuthSessionState.ACTIVE, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MembershipRole] = mapped_column(Enum(MembershipRole), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceSetting(TimestampMixin, Base):
    __tablename__ = "workspace_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(8), default="vi", nullable=False)
    theme: Mapped[str] = mapped_column(String(16), default="dark", nullable=False)
    layout: Mapped[str] = mapped_column(String(16), default="vertical", nullable=False)
    default_delay_min: Mapped[float] = mapped_column(nullable=False, default=15.0)
    default_delay_max: Mapped[float] = mapped_column(nullable=False, default=30.0)
    proxy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxy_scheme: Mapped[str | None] = mapped_column(String(16))
    proxy_host: Mapped[str | None] = mapped_column(String(255))
    proxy_port: Mapped[int | None] = mapped_column(Integer)
    proxy_username: Mapped[str | None] = mapped_column(String(255))
    proxy_password_enc: Mapped[str | None] = mapped_column(Text)


class WorkerNode(Base):
    __tablename__ = "worker_nodes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[WorkerState] = mapped_column(Enum(WorkerState), default=WorkerState.ONLINE, nullable=False)
    app_version: Mapped[str] = mapped_column(String(64), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkspaceSession(TimestampMixin, Base):
    __tablename__ = "workspace_sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    owner_worker_id: Mapped[str] = mapped_column(String(128), ForeignKey("worker_nodes.id"), nullable=False)
    login_state: Mapped[WorkspaceLoginState] = mapped_column(
        Enum(WorkspaceLoginState), default=WorkspaceLoginState.IDLE, nullable=False
    )
    profile_path: Mapped[str] = mapped_column(Text, nullable=False)
    profile_name: Mapped[str | None] = mapped_column(String(255))
    profile_avatar_url: Mapped[str | None] = mapped_column(Text)
    phone_number: Mapped[str | None] = mapped_column(String(32))
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class ContactSyncRun(Base):
    __tablename__ = "contact_sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("automation_jobs.id", ondelete="SET NULL"))
    sync_status: Mapped[str] = mapped_column(String(64), nullable=False)
    contact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stored_contact_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    diagnostics_json: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "identity_key", name="uq_contacts_workspace_identity_key"),
        Index("idx_contacts_workspace_active_name", "workspace_id", "is_active", "normalized_name"),
        Index("idx_contacts_workspace_zid", "workspace_id", "zid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    zid: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    last_message: Mapped[str | None] = mapped_column(Text)
    unread: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    identity_source: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_sync_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("contact_sync_runs.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (Index("idx_campaigns_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    filters_json: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(Enum(CampaignStatus), default=CampaignStatus.DRAFT, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("automation_jobs.id", ondelete="SET NULL"))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignResult(Base):
    __tablename__ = "campaign_results"
    __table_args__ = (UniqueConstraint("campaign_id", "identity_key", name="uq_campaign_results_campaign_identity_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("contacts.id", ondelete="SET NULL"))
    identity_key: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    contact_name: Mapped[str] = mapped_column(Text, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AutomationJob(TimestampMixin, Base):
    __tablename__ = "automation_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_jobs_workspace_idempotency_key"),
        Index("idx_jobs_queue_claim", "status", "run_after", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    type: Mapped[JobType] = mapped_column(Enum(JobType), nullable=False)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, nullable=False)
    failure_class: Mapped[JobFailureClass] = mapped_column(
        Enum(JobFailureClass), default=JobFailureClass.NONE, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONType, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_owner_worker_id: Mapped[str | None] = mapped_column(String(128), ForeignKey("worker_nodes.id", ondelete="SET NULL"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationJobEvent(Base):
    __tablename__ = "automation_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence_no", name="uq_job_events_job_sequence"),
        Index("idx_job_events_job_seq", "job_id", "sequence_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("automation_jobs.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(64))
    success: Mapped[bool | None] = mapped_column(Boolean)
    payload_json: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"))
    actor_type: Mapped[AuditActorType] = mapped_column(Enum(AuditActorType), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    actor_worker_id: Mapped[str | None] = mapped_column(String(128), ForeignKey("worker_nodes.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
