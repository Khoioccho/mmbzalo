from __future__ import annotations

import json
import logging
import socket
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.api_models import (
    AuthSessionResult,
    BootstrapAdminResult,
    CancelJobResult,
    JobEventResult,
    JobListResult,
    JobResult,
    JobSubmissionResult,
    LegacyImportResult,
    ReadinessResult,
    UserSummary,
    WorkspaceSessionResult,
    WorkspaceSummary,
    WorkspaceSwitchResult,
)
from app.config import Settings, get_settings
from app.contact_name_utils import normalize_contact_name
from app.crypto import decrypt_value, encrypt_value
from app.db_models import (
    AuditActorType,
    AuditLog,
    AuthSession,
    AuthSessionState,
    AutomationJob,
    AutomationJobEvent,
    Campaign,
    CampaignResult,
    CampaignStatus,
    Contact,
    ContactSyncRun,
    JobFailureClass,
    JobStatus,
    JobType,
    MembershipRole,
    User,
    WorkerNode,
    WorkerState,
    Workspace,
    WorkspaceLoginState,
    WorkspaceMembership,
    WorkspaceSession,
    WorkspaceSetting,
)
from app.models import (
    AppSettings,
    CampaignContactPreview,
    CampaignDraftPayload,
    CampaignInfo,
    CampaignListResult,
    CampaignOperationResult,
    CampaignProgressEvent,
    CampaignProgressResult,
    ContactInfo,
    ContactListResult,
    ContactQueryParams,
    ContactSyncDiagnostics,
    ContactSyncRunInfo,
    ContactSyncRunListResult,
    LoginStatus,
    MessageResult,
)
from app.proxy_config import parse_proxy_input
from app.security import generate_session_token, hash_password, hash_session_token, session_expiry, utcnow, verify_password


ROLE_ORDER = {
    MembershipRole.VIEWER: 1,
    MembershipRole.OPERATOR: 2,
    MembershipRole.ADMIN: 3,
}

logger = logging.getLogger("services")


def slugify(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug or f"workspace-{uuid.uuid4().hex[:8]}"


def unique_workspace_slug(db: Session, name: str) -> str:
    base_slug = slugify(name)
    slug = base_slug
    suffix = 2
    while db.scalar(select(Workspace.id).where(Workspace.slug == slug)):
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def normalize_email(value: str) -> str:
    return value.strip().lower()


def validate_password_strength(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if not any(char.isalpha() for char in password) or not any(char.isdigit() for char in password):
        raise ValueError("Password must include both letters and numbers.")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def workspace_profile_path(workspace_id: uuid.UUID, settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    return str((cfg.browser_profiles_root / str(workspace_id)).resolve())


def _app_settings_from_record(record: WorkspaceSetting | None) -> AppSettings:
    if not record:
        return AppSettings()

    proxy_raw = None
    if record.proxy_enabled and record.proxy_host and record.proxy_port:
        proxy_raw = f"{record.proxy_host}:{record.proxy_port}"
        if record.proxy_username:
            password = decrypt_value(record.proxy_password_enc)
            if password:
                proxy_raw = f"{record.proxy_username}:{password}@{record.proxy_host}:{record.proxy_port}"
            else:
                proxy_raw = f"{record.proxy_username}@{record.proxy_host}:{record.proxy_port}"

    return AppSettings(
        language=record.language,
        theme=record.theme,
        layout=record.layout,
        proxy_enabled=record.proxy_enabled,
        proxy_raw=proxy_raw,
        proxy_address=record.proxy_host,
        proxy_port=record.proxy_port,
        delay_min=record.default_delay_min,
        delay_max=record.default_delay_max,
    )


def _apply_settings_payload(record: WorkspaceSetting, payload: AppSettings) -> WorkspaceSetting:
    record.language = payload.language
    record.theme = payload.theme
    record.layout = payload.layout
    record.default_delay_min = payload.delay_min
    record.default_delay_max = payload.delay_max
    record.proxy_enabled = payload.proxy_enabled
    record.proxy_scheme = None
    record.proxy_host = None
    record.proxy_port = None
    record.proxy_username = None
    record.proxy_password_enc = None
    if payload.proxy_enabled:
        proxy = parse_proxy_input(payload.proxy_raw) if payload.proxy_raw else None
        if proxy:
            record.proxy_scheme = proxy.scheme
            record.proxy_host = proxy.host
            record.proxy_port = proxy.port
            record.proxy_username = proxy.username
            record.proxy_password_enc = encrypt_value(proxy.password)
        elif payload.proxy_address and payload.proxy_port:
            record.proxy_scheme = "http"
            record.proxy_host = payload.proxy_address
            record.proxy_port = payload.proxy_port
    return record


def ensure_workspace_settings(db: Session, workspace_id: uuid.UUID) -> WorkspaceSetting:
    settings_record = db.get(WorkspaceSetting, workspace_id)
    if settings_record is None:
        settings_record = WorkspaceSetting(workspace_id=workspace_id)
        db.add(settings_record)
        db.flush()
    return settings_record


def ensure_worker_node(db: Session, settings: Settings | None = None) -> WorkerNode:
    cfg = settings or get_settings()
    node = db.get(WorkerNode, cfg.host_identity)
    if node is None:
        node = WorkerNode(
            id=cfg.host_identity,
            hostname=socket.gethostname(),
            state=WorkerState.ONLINE,
            app_version=cfg.app_version,
            last_heartbeat_at=now_utc(),
        )
        db.add(node)
        db.flush()
    return node


def ensure_workspace_session(db: Session, workspace_id: uuid.UUID, settings: Settings | None = None) -> WorkspaceSession:
    cfg = settings or get_settings()
    ensure_worker_node(db, cfg)
    expected_profile_path = workspace_profile_path(workspace_id, cfg)
    session_row = db.get(WorkspaceSession, workspace_id)
    if session_row is None:
        session_row = WorkspaceSession(
            workspace_id=workspace_id,
            owner_worker_id=cfg.host_identity,
            login_state=WorkspaceLoginState.IDLE,
            profile_path=expected_profile_path,
        )
        db.add(session_row)
        db.flush()
        return session_row

    original_owner_worker_id = session_row.owner_worker_id
    original_profile_path = session_row.profile_path
    reset_login_state = False

    if session_row.profile_path != expected_profile_path:
        session_row.profile_path = expected_profile_path
        session_row.login_state = WorkspaceLoginState.IDLE
        session_row.profile_name = None
        session_row.profile_avatar_url = None
        session_row.phone_number = None
        session_row.last_authenticated_at = None
        session_row.last_validated_at = None
        session_row.error_message = None
        reset_login_state = True

    if session_row.owner_worker_id != cfg.host_identity:
        session_row.owner_worker_id = cfg.host_identity

    if reset_login_state or session_row.owner_worker_id != original_owner_worker_id:
        logger.info(
            "Reconciled workspace session workspace_id=%s owner_worker_id=%s->%s profile_path=%s->%s reset_login=%s",
            workspace_id,
            original_owner_worker_id,
            session_row.owner_worker_id,
            original_profile_path,
            session_row.profile_path,
            reset_login_state,
        )
        db.flush()

    return session_row


def serialize_user(user: User) -> UserSummary:
    return UserSummary(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_platform_admin=user.is_platform_admin,
    )


def _serialize_workspace_membership(db: Session, membership: WorkspaceMembership) -> WorkspaceSummary:
    workspace = db.get(Workspace, membership.workspace_id)
    session_row = ensure_workspace_session(db, membership.workspace_id)
    return WorkspaceSummary(
        workspace_id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        role=membership.role,
        login_state=session_row.login_state if session_row else WorkspaceLoginState.IDLE,
    )


def list_workspace_memberships(db: Session, user_id: uuid.UUID) -> list[WorkspaceMembership]:
    return list(
        db.scalars(
            select(WorkspaceMembership).where(WorkspaceMembership.user_id == user_id).order_by(WorkspaceMembership.created_at)
        )
    )


def build_auth_session_result(db: Session, auth_session: AuthSession, user: User) -> AuthSessionResult:
    memberships = list_workspace_memberships(db, user.id)
    workspaces = [_serialize_workspace_membership(db, item) for item in memberships]
    return AuthSessionResult(
        user=serialize_user(user),
        active_workspace_id=auth_session.active_workspace_id,
        workspaces=workspaces,
    )


def create_auth_session(db: Session, user: User, *, ip_address: str | None, user_agent: str | None) -> tuple[str, AuthSession]:
    token = generate_session_token()
    memberships = list_workspace_memberships(db, user.id)
    active_workspace_id = memberships[0].workspace_id if memberships else None
    session_row = AuthSession(
        user_id=user.id,
        active_workspace_id=active_workspace_id,
        session_token_hash=hash_session_token(token),
        state=AuthSessionState.ACTIVE,
        ip_address=ip_address,
        user_agent=user_agent,
        expires_at=session_expiry(),
        last_seen_at=utcnow(),
    )
    user.last_login_at = utcnow()
    db.add(session_row)
    db.flush()
    return token, session_row


def register_user_with_workspace(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    workspace_name: str,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[str, AuthSession, User]:
    normalized_email = normalize_email(email)
    normalized_display_name = display_name.strip()
    normalized_workspace_name = workspace_name.strip()
    if not normalized_email or "@" not in normalized_email or "." not in normalized_email.rsplit("@", 1)[-1]:
        raise ValueError("Enter a valid email address.")
    if not normalized_display_name:
        raise ValueError("Display name is required.")
    if not normalized_workspace_name:
        raise ValueError("Workspace name is required.")
    validate_password_strength(password)
    existing = db.scalar(select(User.id).where(func.lower(User.email) == normalized_email))
    if existing:
        raise ValueError("Registration could not be completed.")

    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        display_name=normalized_display_name,
        is_platform_admin=False,
    )
    db.add(user)
    db.flush()

    workspace = Workspace(
        slug=unique_workspace_slug(db, normalized_workspace_name),
        name=normalized_workspace_name,
        is_active=True,
        created_by_id=user.id,
    )
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.ADMIN))
    ensure_workspace_settings(db, workspace.id)
    ensure_workspace_session(db, workspace.id)
    token, auth_session = create_auth_session(db, user, ip_address=ip_address, user_agent=user_agent)
    auth_session.active_workspace_id = workspace.id
    db.flush()
    return token, auth_session, user


def create_workspace_for_user(
    db: Session,
    *,
    user: User,
    auth_session: AuthSession,
    name: str,
) -> WorkspaceSwitchResult:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Workspace name is required.")
    workspace = Workspace(
        slug=unique_workspace_slug(db, normalized_name),
        name=normalized_name,
        is_active=True,
        created_by_id=user.id,
    )
    db.add(workspace)
    db.flush()
    membership = WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.ADMIN)
    db.add(membership)
    ensure_workspace_settings(db, workspace.id)
    ensure_workspace_session(db, workspace.id)
    auth_session.active_workspace_id = workspace.id
    db.flush()
    return WorkspaceSwitchResult(active_workspace_id=workspace.id, workspace=_serialize_workspace_membership(db, membership))


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def resolve_auth_session(db: Session, raw_token: str | None) -> tuple[AuthSession, User] | None:
    if not raw_token:
        return None
    session_row = db.scalar(select(AuthSession).where(AuthSession.session_token_hash == hash_session_token(raw_token)))
    if not session_row:
        return None
    if session_row.state != AuthSessionState.ACTIVE or session_row.expires_at <= utcnow():
        session_row.state = AuthSessionState.EXPIRED
        db.flush()
        return None
    user = db.get(User, session_row.user_id)
    if not user or not user.is_active:
        return None
    session_row.last_seen_at = utcnow()
    return session_row, user


def revoke_auth_session(db: Session, auth_session: AuthSession) -> None:
    auth_session.state = AuthSessionState.REVOKED
    auth_session.revoked_at = utcnow()
    db.flush()


def switch_active_workspace(db: Session, auth_session: AuthSession, user: User, workspace_id: uuid.UUID) -> WorkspaceSwitchResult:
    membership = db.get(WorkspaceMembership, {"workspace_id": workspace_id, "user_id": user.id})
    if membership is None:
        raise PermissionError("You do not have access to that workspace.")
    auth_session.active_workspace_id = workspace_id
    ensure_workspace_settings(db, workspace_id)
    ensure_workspace_session(db, workspace_id)
    db.flush()
    return WorkspaceSwitchResult(active_workspace_id=workspace_id, workspace=_serialize_workspace_membership(db, membership))


def require_workspace_membership(db: Session, user: User, workspace_id: uuid.UUID | None, minimum_role: MembershipRole | None = None) -> WorkspaceMembership:
    if workspace_id is None:
        raise PermissionError("No active workspace selected.")
    membership = db.get(WorkspaceMembership, {"workspace_id": workspace_id, "user_id": user.id})
    if membership is None:
        raise PermissionError("You do not have access to that workspace.")
    if minimum_role and ROLE_ORDER[membership.role] < ROLE_ORDER[minimum_role]:
        raise PermissionError("You do not have permission to perform that action.")
    return membership


def get_workspace_settings(db: Session, workspace_id: uuid.UUID) -> AppSettings:
    return _app_settings_from_record(ensure_workspace_settings(db, workspace_id))


def update_workspace_settings(db: Session, workspace_id: uuid.UUID, payload: AppSettings) -> AppSettings:
    record = ensure_workspace_settings(db, workspace_id)
    _apply_settings_payload(record, payload)
    db.flush()
    return _app_settings_from_record(record)


def _contact_to_model(contact: Contact) -> ContactInfo:
    return ContactInfo(
        zid=contact.zid,
        name=contact.name,
        phone=contact.phone,
        avatar_url=contact.avatar_url,
        last_message=contact.last_message,
        unread=contact.unread,
        identity_key=contact.identity_key,
        identity_source=contact.identity_source,
        last_seen_at=contact.last_seen_at.isoformat() if contact.last_seen_at else None,
    )


def _sync_run_to_model(run: ContactSyncRun) -> ContactSyncRunInfo:
    return ContactSyncRunInfo(
        sync_run_id=run.id,
        sync_status=run.sync_status,
        contact_count=run.contact_count,
        stored_contact_count=run.stored_contact_count,
        message=run.message,
        timestamp=run.finished_at.isoformat() if run.finished_at else run.started_at.isoformat(),
        diagnostics=ContactSyncDiagnostics(**(run.diagnostics_json or {})),
    )


def list_contacts(db: Session, workspace_id: uuid.UUID, filters: ContactQueryParams | None = None) -> ContactListResult:
    filters = filters or ContactQueryParams()
    query: Select[tuple[Contact]] = select(Contact).where(Contact.workspace_id == workspace_id, Contact.is_active.is_(True))
    if filters.search:
        term = f"%{filters.search.strip().lower()}%"
        query = query.where(func.lower(Contact.name).like(term))
    if filters.unread_only:
        query = query.where(Contact.unread.is_(True))
    if filters.identity_source in {"zid", "name_avatar"}:
        query = query.where(Contact.identity_source == filters.identity_source)
    if filters.selected_ids:
        query = query.where(Contact.identity_key.in_(filters.selected_ids))

    sort_column = Contact.name if filters.sort_by == "name" else Contact.last_seen_at
    query = query.order_by(sort_column.desc() if filters.sort_order == "desc" else sort_column.asc(), Contact.id.asc())

    contacts = list(db.scalars(query))
    total_stored = db.scalar(
        select(func.count()).select_from(Contact).where(Contact.workspace_id == workspace_id, Contact.is_active.is_(True))
    ) or 0
    last_run = db.scalar(
        select(ContactSyncRun).where(ContactSyncRun.workspace_id == workspace_id).order_by(ContactSyncRun.id.desc()).limit(1)
    )
    return ContactListResult(
        contacts=[_contact_to_model(item) for item in contacts],
        contact_count=len(contacts),
        stored_contact_count=int(total_stored),
        sync_status="stored" if total_stored else "idle",
        sync_run_id=last_run.id if last_run else None,
        last_sync_at=(last_run.finished_at or last_run.started_at).isoformat() if last_run else None,
        last_sync_status=last_run.sync_status if last_run else None,
        diagnostics=ContactSyncDiagnostics(**((last_run.diagnostics_json or {}) if last_run else {})),
        message=_stored_contacts_message(last_run, len(contacts), filters),
    )


def _stored_contacts_message(last_run: ContactSyncRun | None, count: int, filters: ContactQueryParams) -> str:
    if not last_run:
        return "No contacts have been stored yet. Run a sync job to persist your Zalo friend list."
    prefix = f"Loaded {count} stored contact(s)."
    if filters.search or filters.unread_only or filters.identity_source != "all" or filters.selected_ids:
        prefix = f"Loaded {count} filtered contact(s)."
    finished_at = last_run.finished_at or last_run.started_at
    return f"{prefix} Last sync: {last_run.sync_status} at {finished_at.isoformat()}."


def list_sync_runs(db: Session, workspace_id: uuid.UUID, limit: int = 20) -> ContactSyncRunListResult:
    runs = list(
        db.scalars(
            select(ContactSyncRun).where(ContactSyncRun.workspace_id == workspace_id).order_by(ContactSyncRun.id.desc()).limit(limit)
        )
    )
    return ContactSyncRunListResult(runs=[_sync_run_to_model(item) for item in runs], total=len(runs))


def _resolve_campaign_contacts(db: Session, workspace_id: uuid.UUID, selected_ids: list[str]) -> list[Contact]:
    ordered_ids = list(dict.fromkeys(item for item in selected_ids if item))
    if not ordered_ids:
        return []
    rows = list(
        db.scalars(
            select(Contact).where(
                Contact.workspace_id == workspace_id,
                Contact.is_active.is_(True),
                Contact.identity_key.in_(ordered_ids),
            )
        )
    )
    order = {identity_key: index for index, identity_key in enumerate(ordered_ids)}
    rows.sort(key=lambda item: order.get(item.identity_key, len(order)))
    return rows


def _campaign_to_model(db: Session, campaign: Campaign) -> CampaignInfo:
    filters = ContactQueryParams(**(campaign.filters_json or {}))
    results_rows = list(
        db.scalars(select(CampaignResult).where(CampaignResult.campaign_id == campaign.id).order_by(CampaignResult.id.asc()))
    )
    matched_contacts = []
    if filters.selected_ids:
        contacts = _resolve_campaign_contacts(db, campaign.workspace_id, filters.selected_ids)
        matched_contacts = [
            CampaignContactPreview(
                identity_key=item.identity_key,
                name=item.name,
                avatar_url=item.avatar_url,
                unread=item.unread,
                identity_source=item.identity_source,
                last_seen_at=item.last_seen_at.isoformat() if item.last_seen_at else None,
            )
            for item in contacts
        ]
    return CampaignInfo(
        campaign_id=campaign.id,
        name=campaign.name,
        message=campaign.message,
        filters=filters,
        selected_contact_ids=filters.selected_ids,
        matched_contacts=matched_contacts,
        matched_count=campaign.matched_count,
        status=campaign.status.value,
        sent_count=campaign.sent_count,
        failed_count=campaign.failed_count,
        results=[
            {
                "identity_key": item.identity_key,
                "target": item.target,
                "name": item.contact_name,
                "success": item.success,
                "error": item.error_message,
            }
            for item in results_rows
        ],
        created_at=campaign.created_at.isoformat(),
        executed_at=campaign.executed_at.isoformat() if campaign.executed_at else None,
    )


def create_campaign(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID, payload: CampaignDraftPayload) -> CampaignOperationResult:
    filters = payload.filters
    selected_ids = list(dict.fromkeys(filters.selected_ids))
    contacts = _resolve_campaign_contacts(db, workspace_id, selected_ids)
    campaign = Campaign(
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        name=payload.name.strip(),
        message=payload.message,
        filters_json=filters.model_dump(),
        status=CampaignStatus.DRAFT,
        matched_count=len(contacts),
    )
    db.add(campaign)
    db.flush()
    return CampaignOperationResult(
        campaign=_campaign_to_model(db, campaign),
        message=f"Campaign '{campaign.name}' saved with {campaign.matched_count} matched contact(s).",
    )


def list_campaigns(db: Session, workspace_id: uuid.UUID, limit: int = 20) -> CampaignListResult:
    campaigns = list(
        db.scalars(select(Campaign).where(Campaign.workspace_id == workspace_id).order_by(Campaign.id.desc()).limit(limit))
    )
    return CampaignListResult(campaigns=[_campaign_to_model(db, item) for item in campaigns], total=len(campaigns))


def get_campaign(db: Session, workspace_id: uuid.UUID, campaign_id: int) -> Campaign:
    campaign = db.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.workspace_id == workspace_id))
    if campaign is None:
        raise ValueError("Campaign not found.")
    return campaign


def create_job(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: JobType,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> JobSubmissionResult:
    active_job = db.scalar(
        select(AutomationJob).where(
            AutomationJob.workspace_id == workspace_id,
            AutomationJob.status.in_([JobStatus.QUEUED, JobStatus.LEASED, JobStatus.RUNNING]),
        )
    )
    if active_job:
        raise ValueError("This workspace already has an active automation job.")

    job = AutomationJob(
        workspace_id=workspace_id,
        created_by_user_id=user_id,
        type=job_type,
        status=JobStatus.QUEUED,
        failure_class=JobFailureClass.NONE,
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        payload_json=payload,
        max_attempts=get_settings().job_max_attempts,
    )
    db.add(job)
    db.flush()
    create_job_event(db, job, event_type="queued", message=f"{job_type.value} job queued.", payload={})
    return JobSubmissionResult(
        job_id=job.id,
        workspace_id=job.workspace_id,
        status=job.status,
        type=job.type,
        message=f"{job.type.value} job queued.",
        created_at=job.created_at,
    )


def create_job_event(
    db: Session,
    job: AutomationJob,
    *,
    event_type: str,
    message: str,
    level: str = "info",
    target: str | None = None,
    route: str | None = None,
    success: bool | None = None,
    payload: dict[str, Any] | None = None,
) -> AutomationJobEvent:
    current_max = db.scalar(select(func.max(AutomationJobEvent.sequence_no)).where(AutomationJobEvent.job_id == job.id))
    sequence_no = int(current_max or 0) + 1
    event = AutomationJobEvent(
        job_id=job.id,
        workspace_id=job.workspace_id,
        sequence_no=sequence_no,
        level=level,
        event_type=event_type,
        message=message,
        target=target,
        route=route,
        success=success,
        payload_json=payload or {},
    )
    db.add(event)
    db.flush()
    return event


def _job_to_result(db: Session, job: AutomationJob, include_events: bool = True) -> JobResult:
    events = []
    if include_events:
        events_rows = list(
            db.scalars(select(AutomationJobEvent).where(AutomationJobEvent.job_id == job.id).order_by(AutomationJobEvent.sequence_no))
        )
        events = [
            JobEventResult(
                sequence_no=item.sequence_no,
                level=item.level,
                event_type=item.event_type,
                message=item.message,
                target=item.target,
                route=item.route,
                success=item.success,
                payload=item.payload_json or {},
                created_at=item.created_at,
            )
            for item in events_rows
        ]
    return JobResult(
        job_id=job.id,
        workspace_id=job.workspace_id,
        type=job.type,
        status=job.status,
        failure_class=job.failure_class,
        cancel_requested=job.cancel_requested,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        payload=job.payload_json or {},
        result=job.result_json or {},
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        events=events,
    )


def list_jobs(db: Session, workspace_id: uuid.UUID, limit: int = 50) -> JobListResult:
    jobs = list(
        db.scalars(select(AutomationJob).where(AutomationJob.workspace_id == workspace_id).order_by(AutomationJob.created_at.desc()).limit(limit))
    )
    return JobListResult(jobs=[_job_to_result(db, item, include_events=False) for item in jobs], total=len(jobs))


def get_job(db: Session, workspace_id: uuid.UUID, job_id: uuid.UUID) -> JobResult:
    job = db.scalar(select(AutomationJob).where(AutomationJob.id == job_id, AutomationJob.workspace_id == workspace_id))
    if job is None:
        raise ValueError("Job not found.")
    return _job_to_result(db, job, include_events=True)


def cancel_job(db: Session, workspace_id: uuid.UUID, job_id: uuid.UUID) -> CancelJobResult:
    job = db.scalar(select(AutomationJob).where(AutomationJob.id == job_id, AutomationJob.workspace_id == workspace_id))
    if job is None:
        raise ValueError("Job not found.")
    if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
        return CancelJobResult(job_id=job.id, status=job.status, cancel_requested=job.cancel_requested)
    job.cancel_requested = True
    create_job_event(db, job, event_type="cancel_requested", message="Cancellation requested.")
    db.flush()
    return CancelJobResult(job_id=job.id, status=job.status, cancel_requested=job.cancel_requested)


def get_campaign_progress(db: Session, workspace_id: uuid.UUID, campaign_id: int) -> CampaignProgressResult:
    campaign = get_campaign(db, workspace_id, campaign_id)
    if not campaign.last_job_id:
        return CampaignProgressResult(campaign_id=campaign_id, status="idle", events=[])
    job = db.scalar(select(AutomationJob).where(AutomationJob.id == campaign.last_job_id))
    if job is None:
        return CampaignProgressResult(campaign_id=campaign_id, status="idle", events=[])
    events = list(
        db.scalars(select(AutomationJobEvent).where(AutomationJobEvent.job_id == job.id).order_by(AutomationJobEvent.sequence_no))
    )
    sent = sum(1 for item in events if item.event_type == "target_done" and item.success)
    failed = sum(1 for item in events if item.event_type == "target_done" and item.success is False)
    current = None
    for item in reversed(events):
        if item.event_type == "target_start":
            current = item.target
            break
    return CampaignProgressResult(
        campaign_id=campaign_id,
        status=job.status.value,
        total=campaign.matched_count,
        sent=sent,
        failed=failed,
        current=current,
        events=[
            CampaignProgressEvent(
                sequence=item.sequence_no,
                message=item.message,
                level=item.level,
                target=item.target,
                route=item.route,
                success=item.success,
                timestamp=item.created_at.isoformat(),
            )
            for item in events
        ],
    )


def get_workspace_session_result(db: Session, workspace_id: uuid.UUID) -> WorkspaceSessionResult:
    session_row = ensure_workspace_session(db, workspace_id)
    return WorkspaceSessionResult(
        workspace_id=session_row.workspace_id,
        owner_worker_id=session_row.owner_worker_id,
        login_state=session_row.login_state,
        profile_path=session_row.profile_path,
        profile_name=session_row.profile_name,
        profile_avatar_url=session_row.profile_avatar_url,
        phone_number=session_row.phone_number,
        last_authenticated_at=session_row.last_authenticated_at,
        last_validated_at=session_row.last_validated_at,
        error_message=session_row.error_message,
    )


def update_workspace_login_status(db: Session, workspace_id: uuid.UUID, login_status: LoginStatus) -> WorkspaceSession:
    row = ensure_workspace_session(db, workspace_id)
    row.login_state = WorkspaceLoginState(login_status.state.value if hasattr(login_status.state, "value") else login_status.state)
    row.profile_name = login_status.profile_name
    row.profile_avatar_url = login_status.profile_avatar
    row.phone_number = login_status.phone_number
    row.error_message = login_status.message if row.login_state == WorkspaceLoginState.ERROR else None
    row.last_validated_at = utcnow()
    if row.login_state == WorkspaceLoginState.AUTHENTICATED:
        row.last_authenticated_at = utcnow()
    db.flush()
    return row


def bootstrap_admin(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str,
    workspace_name: str,
    workspace_slug: str | None = None,
) -> BootstrapAdminResult:
    existing = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if existing:
        raise ValueError("User already exists.")
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        display_name=display_name.strip(),
        is_platform_admin=True,
    )
    db.add(user)
    db.flush()
    workspace = Workspace(
        slug=workspace_slug or unique_workspace_slug(db, workspace_name),
        name=workspace_name.strip(),
        is_active=True,
        created_by_id=user.id,
    )
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.ADMIN))
    ensure_workspace_settings(db, workspace.id)
    ensure_workspace_session(db, workspace.id)
    db.flush()
    return BootstrapAdminResult(
        user_id=user.id,
        workspace_id=workspace.id,
        email=user.email,
        workspace_slug=workspace.slug,
    )


def import_legacy_workspace_data(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID) -> LegacyImportResult:
    settings = get_settings()
    imported_contacts = 0
    imported_sync_runs = 0
    imported_campaigns = 0
    imported_settings = False

    if settings.legacy_settings_path.exists():
        payload = json.loads(settings.legacy_settings_path.read_text(encoding="utf-8"))
        update_workspace_settings(db, workspace_id, AppSettings(**payload))
        imported_settings = True

    if settings.legacy_contacts_db_path.exists():
        conn = sqlite3.connect(settings.legacy_contacts_db_path)
        conn.row_factory = sqlite3.Row
        try:
            contact_rows = conn.execute("SELECT * FROM contacts").fetchall()
            sync_rows = conn.execute("SELECT * FROM contact_sync_runs").fetchall()
            campaign_rows = conn.execute("SELECT * FROM campaigns").fetchall()
        finally:
            conn.close()

        for row in sync_rows:
            run = ContactSyncRun(
                workspace_id=workspace_id,
                requested_by_user_id=user_id,
                sync_status=row["sync_status"],
                contact_count=row["contact_count"],
                stored_contact_count=row["stored_contact_count"],
                message=row["message"],
                diagnostics_json=json.loads(row["diagnostics_json"] or "{}"),
                started_at=datetime.fromisoformat(row["timestamp"]),
                finished_at=datetime.fromisoformat(row["timestamp"]),
            )
            db.add(run)
            db.flush()
            imported_sync_runs += 1

        last_sync_run = db.scalar(
            select(ContactSyncRun).where(ContactSyncRun.workspace_id == workspace_id).order_by(ContactSyncRun.id.desc()).limit(1)
        )
        for row in contact_rows:
            contact = Contact(
                workspace_id=workspace_id,
                identity_key=row["identity_key"],
                zid=row["zid"],
                name=row["name"],
                normalized_name=normalize_contact_name(row["name"]) or row["name"].strip().lower(),
                phone=row["phone"],
                avatar_url=row["avatar_url"],
                last_message=row["last_message"],
                unread=bool(row["unread"]),
                identity_source=row["identity_source"],
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
                last_seen_sync_run_id=last_sync_run.id if last_sync_run else None,
                is_active=bool(row["is_active"]),
            )
            db.add(contact)
            imported_contacts += 1

        for row in campaign_rows:
            campaign = Campaign(
                workspace_id=workspace_id,
                created_by_user_id=user_id,
                name=row["name"],
                message=row["message"],
                filters_json=json.loads(row["filters_json"] or "{}"),
                status=CampaignStatus(row["status"]) if row["status"] in CampaignStatus._value2member_map_ else CampaignStatus.DRAFT,
                matched_count=row["matched_count"],
                sent_count=row["sent_count"],
                failed_count=row["failed_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
                executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
            )
            db.add(campaign)
            db.flush()
            for item in json.loads(row["results_json"] or "[]"):
                db.add(
                    CampaignResult(
                        campaign_id=campaign.id,
                        workspace_id=workspace_id,
                        identity_key=item["identity_key"],
                        target=item["target"],
                        contact_name=item["name"],
                        success=bool(item["success"]),
                        error_message=item.get("error"),
                        sent_at=campaign.executed_at,
                    )
                )
            imported_campaigns += 1

    db.flush()
    return LegacyImportResult(
        workspace_id=workspace_id,
        imported_contacts=imported_contacts,
        imported_sync_runs=imported_sync_runs,
        imported_campaigns=imported_campaigns,
        imported_settings=imported_settings,
    )


def record_audit_log(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor_type: AuditActorType,
    workspace_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_worker_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            actor_worker_id=actor_worker_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata or {},
        )
    )
    db.flush()


def heartbeat_worker(db: Session, settings: Settings | None = None) -> WorkerNode:
    node = ensure_worker_node(db, settings)
    node.last_heartbeat_at = now_utc()
    node.state = WorkerState.ONLINE
    db.flush()
    return node


def claim_next_job(db: Session, settings: Settings | None = None) -> AutomationJob | None:
    cfg = settings or get_settings()
    heartbeat_worker(db, cfg)
    stmt = (
        select(AutomationJob)
        .where(
            AutomationJob.status == JobStatus.QUEUED,
            AutomationJob.run_after <= now_utc(),
        )
        .order_by(AutomationJob.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = db.scalar(stmt)
    if job is None:
        return None
    job.status = JobStatus.LEASED
    job.lease_owner_worker_id = cfg.host_identity
    job.lease_expires_at = now_utc() + timedelta(seconds=cfg.job_lease_seconds)
    job.attempt_count += 1
    create_job_event(db, job, event_type="leased", message=f"Job leased by {cfg.host_identity}.")
    db.flush()
    return job


def mark_job_running(db: Session, job: AutomationJob) -> None:
    job.status = JobStatus.RUNNING
    job.started_at = job.started_at or now_utc()
    create_job_event(db, job, event_type="started", message=f"{job.type.value} job started.")
    db.flush()


def renew_job_lease(db: Session, job: AutomationJob, settings: Settings | None = None) -> None:
    cfg = settings or get_settings()
    job.lease_expires_at = now_utc() + timedelta(seconds=cfg.job_lease_seconds)
    db.flush()


def mark_job_cancelled(db: Session, job: AutomationJob, message: str) -> None:
    job.status = JobStatus.CANCELLED
    job.finished_at = now_utc()
    job.result_json = {"message": message}
    create_job_event(db, job, event_type="cancelled", message=message, level="error")
    db.flush()


def mark_job_failed(
    db: Session,
    job: AutomationJob,
    *,
    message: str,
    failure_class: JobFailureClass = JobFailureClass.PERMANENT,
    result: dict[str, Any] | None = None,
) -> None:
    job.status = JobStatus.FAILED
    job.failure_class = failure_class
    job.finished_at = now_utc()
    job.result_json = result or {"message": message}
    create_job_event(db, job, event_type="failed", message=message, level="error")
    db.flush()


def mark_job_succeeded(db: Session, job: AutomationJob, result: dict[str, Any]) -> None:
    job.status = JobStatus.SUCCEEDED
    job.failure_class = JobFailureClass.NONE
    job.finished_at = now_utc()
    job.result_json = result
    create_job_event(db, job, event_type="completed", message=result.get("message", "Job completed."))
    db.flush()


def persist_synced_contacts(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID | None, job: AutomationJob, sync_result: dict) -> ContactListResult:
    timestamp = sync_result.get("timestamp") or now_utc().isoformat()
    timestamp_dt = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else now_utc()
    diagnostics = ContactSyncDiagnostics(**(sync_result.get("diagnostics") or {}))
    raw_contacts = sync_result.get("contacts") or []
    run = ContactSyncRun(
        workspace_id=workspace_id,
        requested_by_user_id=user_id,
        job_id=job.id,
        sync_status=sync_result.get("sync_status") or "unknown",
        contact_count=len(raw_contacts),
        stored_contact_count=0,
        message=sync_result.get("message") or "",
        diagnostics_json=diagnostics.model_dump(),
        started_at=job.started_at or now_utc(),
        finished_at=now_utc(),
    )
    db.add(run)
    db.flush()

    seen_keys: list[str] = []
    for raw in raw_contacts:
        contact_data = ContactInfo(**raw)
        normalized_name = normalize_contact_name(contact_data.name) or contact_data.name
        identity_key = contact_data.identity_key or (f"id:{contact_data.zid}" if contact_data.zid else f"name_avatar:{normalized_name.lower()}|{(contact_data.avatar_url or '').strip().lower()}")
        seen_keys.append(identity_key)
        row = db.scalar(
            select(Contact).where(Contact.workspace_id == workspace_id, Contact.identity_key == identity_key)
        )
        if row is None:
            row = Contact(
                workspace_id=workspace_id,
                identity_key=identity_key,
                normalized_name=normalized_name.lower(),
                last_seen_at=timestamp_dt,
            )
            db.add(row)
        row.zid = contact_data.zid
        row.name = normalized_name
        row.normalized_name = normalized_name.lower()
        row.phone = contact_data.phone
        row.avatar_url = contact_data.avatar_url
        row.last_message = contact_data.last_message
        row.unread = contact_data.unread
        row.identity_source = contact_data.identity_source or ("zid" if contact_data.zid else "name_avatar")
        row.last_seen_at = timestamp_dt
        row.last_seen_sync_run_id = run.id
        row.is_active = True

    if sync_result.get("sync_status") == "success":
        db.query(Contact).filter(
            Contact.workspace_id == workspace_id,
            Contact.identity_key.not_in(seen_keys if seen_keys else ["__none__"]),
        ).update({"is_active": False}, synchronize_session=False)

    stored_count = db.scalar(
        select(func.count()).select_from(Contact).where(Contact.workspace_id == workspace_id, Contact.is_active.is_(True))
    ) or 0
    run.stored_contact_count = int(stored_count)
    db.flush()
    return list_contacts(db, workspace_id, ContactQueryParams())


def prepare_campaign_job(db: Session, workspace_id: uuid.UUID, campaign_id: int) -> tuple[Campaign, list[Contact]]:
    campaign = get_campaign(db, workspace_id, campaign_id)
    filters = ContactQueryParams(**(campaign.filters_json or {}))
    contacts = _resolve_campaign_contacts(db, workspace_id, filters.selected_ids)
    if not contacts:
        raise ValueError("Campaign has no matched contacts to execute.")
    campaign.matched_count = len(contacts)
    campaign.status = CampaignStatus.QUEUED
    db.flush()
    return campaign, contacts


def persist_campaign_execution(
    db: Session,
    *,
    campaign: Campaign,
    contacts: list[Contact],
    job: AutomationJob,
    send_result: dict,
) -> CampaignInfo:
    db.query(CampaignResult).filter(CampaignResult.campaign_id == campaign.id).delete(synchronize_session=False)
    identity_lookup = {item.get("identity_key"): item for item in send_result.get("results", []) if item.get("identity_key")}
    target_lookup = {item.get("target"): item for item in send_result.get("results", [])}
    sent_at = now_utc()
    for contact in contacts:
        outcome = identity_lookup.get(contact.identity_key) or target_lookup.get(contact.name, {})
        db.add(
            CampaignResult(
                campaign_id=campaign.id,
                workspace_id=campaign.workspace_id,
                contact_id=contact.id,
                identity_key=contact.identity_key,
                target=outcome.get("target") or contact.name,
                contact_name=contact.name,
                success=bool(outcome.get("success")),
                error_code=(outcome.get("error") or "").split(":")[0] if outcome.get("error") else None,
                error_message=outcome.get("error"),
                sent_at=sent_at if outcome.get("success") else None,
            )
        )
    campaign.last_job_id = job.id
    campaign.executed_at = sent_at
    campaign.sent_count = int(send_result.get("sent", 0))
    campaign.failed_count = int(send_result.get("failed", 0))
    campaign.status = (
        CampaignStatus.COMPLETED if campaign.failed_count == 0 else CampaignStatus.COMPLETED_WITH_FAILURES
    )
    db.flush()
    return _campaign_to_model(db, campaign)


def record_campaign_job_event(db: Session, job: AutomationJob, event: dict[str, Any]) -> None:
    create_job_event(
        db,
        job,
        event_type=event.get("event", "info"),
        level=event.get("level", "info"),
        message=event.get("message", ""),
        target=event.get("target"),
        route=event.get("route"),
        success=event.get("success"),
        payload=event,
    )


def get_workspace_runtime_settings(db: Session, workspace_id: uuid.UUID) -> AppSettings:
    return get_workspace_settings(db, workspace_id)


def get_readiness(db: Session, settings: Settings | None = None) -> ReadinessResult:
    cfg = settings or get_settings()
    db.execute(text("SELECT 1"))
    worker = db.get(WorkerNode, cfg.host_identity)
    worker_state = "missing"
    if worker:
        age = now_utc() - worker.last_heartbeat_at
        worker_state = "ok" if age.total_seconds() < (cfg.worker_heartbeat_interval_seconds * 3) else "stale"
    return ReadinessResult(
        status="ok" if worker_state == "ok" else "degraded",
        database="ok",
        worker=worker_state,
        host_identity=cfg.host_identity,
    )
