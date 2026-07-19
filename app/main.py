"""
FastAPI application for the PostgreSQL-backed multi-workspace MMBZalo service.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api_models import (
    AuthSessionResult,
    CancelJobResult,
    CreateWorkspaceRequest,
    JobListResult,
    JobResult,
    JobSubmissionResult,
    LoginRequest,
    ReadinessResult,
    RegisterRequest,
    WorkspaceSessionResult,
    WorkspaceSummary,
    WorkspaceSwitchResult,
)
from app.config import get_settings
from app.database import get_db, session_scope
from app.db_models import AuditActorType, CampaignStatus, JobType, MembershipRole, WorkspaceLoginState
from app.dependencies import RequestContext, get_request_context, require_role
from app.models import (
    AppSettings,
    CampaignDraftPayload,
    CampaignExecutePayload,
    CampaignListResult,
    CampaignOperationResult,
    CampaignProgressResult,
    ContactListResult,
    ContactQueryParams,
    ContactSyncRunListResult,
    FriendRequestPayload,
    GroupMessagePayload,
    LoginState,
    LoginStatus,
    MessagePayload,
)
from app.services import (
    authenticate_user,
    build_auth_session_result,
    cancel_job,
    create_auth_session,
    create_campaign,
    create_job,
    create_workspace_for_user,
    ensure_workspace_session,
    get_campaign,
    get_campaign_progress,
    get_job,
    get_readiness,
    get_workspace_runtime_settings,
    get_workspace_session_result,
    get_workspace_settings,
    list_campaigns,
    list_contacts,
    list_jobs,
    list_sync_runs,
    record_audit_log,
    register_user_with_workspace,
    resolve_auth_session,
    revoke_auth_session,
    switch_active_workspace,
    update_workspace_login_status,
    update_workspace_settings,
)
from app.rate_limit import rate_limiter
from app.zalo_driver import get_driver, shutdown_all_drivers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("api")
settings = get_settings()
LOGIN_WATCH_TIMEOUT_SECONDS = 600
_login_watch_tasks: dict[UUID, asyncio.Task] = {}


def _cookie_value(request: Request) -> str | None:
    return request.cookies.get(settings.session_cookie_name)


def _client_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip() or "unknown"
    return request.client.host if request.client and request.client.host else "unknown"


def _enforce_rate_limit(request: Request, action: str, limit: int) -> None:
    if not rate_limiter.allow(action=action, key=_client_key(request), limit=limit):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Try again later.")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def _driver_settings_provider(workspace_id: UUID):
    def provider() -> AppSettings:
        with session_scope() as db:
            return get_workspace_runtime_settings(db, workspace_id)

    return provider


async def _workspace_driver(workspace_id: UUID) -> object:
    with session_scope() as db:
        ensure_workspace_session(db, workspace_id, settings)
    return await get_driver(str(workspace_id), settings, _driver_settings_provider(workspace_id))


async def _watch_workspace_login(workspace_id: UUID) -> None:
    deadline = asyncio.get_running_loop().time() + LOGIN_WATCH_TIMEOUT_SECONDS
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.5)
            driver = await _workspace_driver(workspace_id)
            result = LoginStatus(**(await driver.check_login_status()))
            with session_scope() as db:
                update_workspace_login_status(db, workspace_id, result)
            if result.state != LoginState.WAITING_QR:
                return

        driver = await _workspace_driver(workspace_id)
        result = LoginStatus(**(await driver.stop_login()))
        result.message = "Zalo login timed out. Start a new connection to receive a fresh QR code."
        with session_scope() as db:
            update_workspace_login_status(db, workspace_id, result)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Workspace login watcher failed workspace_id=%s", workspace_id)
    finally:
        current_task = asyncio.current_task()
        if _login_watch_tasks.get(workspace_id) is current_task:
            _login_watch_tasks.pop(workspace_id, None)


def _start_workspace_login_watcher(workspace_id: UUID) -> None:
    existing = _login_watch_tasks.pop(workspace_id, None)
    if existing:
        existing.cancel()
    _login_watch_tasks[workspace_id] = asyncio.create_task(
        _watch_workspace_login(workspace_id),
        name=f"zalo-login-{workspace_id}",
    )


def _cancel_workspace_login_watcher(workspace_id: UUID) -> None:
    task = _login_watch_tasks.pop(workspace_id, None)
    if task:
        task.cancel()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MMBZalo production service started.")
    yield
    tasks = list(_login_watch_tasks.values())
    _login_watch_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await shutdown_all_drivers()
    logger.info("Shut down complete.")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(settings.frontend_dir)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(settings.frontend_dir / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": settings.app_version}


@app.get("/api/readiness", response_model=ReadinessResult)
async def readiness(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return get_readiness(db)
    except Exception as exc:
        logger.exception("Readiness check failed")
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/auth/login", response_model=AuthSessionResult)
async def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, "auth.login", settings.login_rate_limit_per_hour)
    user = authenticate_user(db, payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")
    token, auth_session = create_auth_session(
        db,
        user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    result = build_auth_session_result(db, auth_session, user)
    _set_session_cookie(response, token)
    record_audit_log(
        db,
        action="auth.login",
        entity_type="user",
        entity_id=str(user.id),
        actor_type=AuditActorType.USER,
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    return result


@app.post("/api/auth/register", response_model=AuthSessionResult)
async def register(payload: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.registration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration is currently disabled.")
    _enforce_rate_limit(request, "auth.register", settings.registration_rate_limit_per_hour)
    try:
        token, auth_session, user = register_user_with_workspace(
            db,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            workspace_name=payload.workspace_name,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    result = build_auth_session_result(db, auth_session, user)
    _set_session_cookie(response, token)
    record_audit_log(
        db,
        workspace_id=auth_session.active_workspace_id,
        action="auth.register",
        entity_type="user",
        entity_id=str(user.id),
        actor_type=AuditActorType.USER,
        actor_user_id=user.id,
        metadata={"email": user.email},
    )
    return result


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    resolved = resolve_auth_session(db, _cookie_value(request))
    if resolved:
        auth_session, user = resolved
        revoke_auth_session(db, auth_session)
        record_audit_log(
            db,
            action="auth.logout",
            entity_type="user",
            entity_id=str(user.id),
            actor_type=AuditActorType.USER,
            actor_user_id=user.id,
        )
    response.delete_cookie(settings.session_cookie_name, domain=settings.cookie_domain, path="/")
    return {"status": "ok"}


@app.get("/api/auth/me", response_model=AuthSessionResult)
async def auth_me(request: Request, db: Session = Depends(get_db)):
    resolved = resolve_auth_session(db, _cookie_value(request))
    if not resolved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    auth_session, user = resolved
    return build_auth_session_result(db, auth_session, user)


@app.get("/api/workspaces", response_model=list[WorkspaceSummary])
async def workspace_list(request: Request, db: Session = Depends(get_db)):
    resolved = resolve_auth_session(db, _cookie_value(request))
    if not resolved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    auth_session, user = resolved
    return build_auth_session_result(db, auth_session, user).workspaces


@app.post("/api/workspaces", response_model=WorkspaceSwitchResult)
async def workspace_create(payload: CreateWorkspaceRequest, request: Request, db: Session = Depends(get_db)):
    resolved = resolve_auth_session(db, _cookie_value(request))
    if not resolved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    auth_session, user = resolved
    try:
        result = create_workspace_for_user(db, user=user, auth_session=auth_session, name=payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    record_audit_log(
        db,
        workspace_id=result.active_workspace_id,
        action="workspace.create",
        entity_type="workspace",
        entity_id=str(result.active_workspace_id),
        actor_type=AuditActorType.USER,
        actor_user_id=user.id,
        metadata={"name": result.workspace.name},
    )
    return result


@app.post("/api/workspaces/{workspace_id}/switch", response_model=WorkspaceSwitchResult)
async def workspace_switch(
    workspace_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    resolved = resolve_auth_session(db, _cookie_value(request))
    if not resolved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    auth_session, user = resolved
    try:
        result = switch_active_workspace(db, auth_session, user, workspace_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return result


@app.get("/api/login/status", response_model=LoginStatus)
async def login_status(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    driver = await _workspace_driver(context.active_workspace_id)
    result = LoginStatus(**(await driver.check_login_status()))
    stored_session = get_workspace_session_result(db, context.active_workspace_id)
    if result.state == LoginState.IDLE and stored_session.login_state == WorkspaceLoginState.AUTHENTICATED:
        return LoginStatus(
            state=LoginState.AUTHENTICATED,
            profile_name=stored_session.profile_name,
            profile_avatar=stored_session.profile_avatar_url,
            phone_number=stored_session.phone_number,
            message="Stored workspace session is authenticated and ready.",
        )
    update_workspace_login_status(db, context.active_workspace_id, result)
    return result


@app.post("/api/login/start", response_model=LoginStatus)
async def login_start(
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    driver = await _workspace_driver(context.active_workspace_id)
    result = LoginStatus(**(await driver.start_login()))
    update_workspace_login_status(db, context.active_workspace_id, result)
    record_audit_log(
        db,
        workspace_id=context.active_workspace_id,
        action="workspace.login.start",
        entity_type="workspace_session",
        entity_id=str(context.active_workspace_id),
        actor_type=AuditActorType.USER,
        actor_user_id=context.user.id,
    )
    _start_workspace_login_watcher(context.active_workspace_id)
    return result


@app.post("/api/login/stop", response_model=LoginStatus)
async def login_stop(
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    _cancel_workspace_login_watcher(context.active_workspace_id)
    driver = await _workspace_driver(context.active_workspace_id)
    result = LoginStatus(**(await driver.stop_login()))
    update_workspace_login_status(db, context.active_workspace_id, result)
    return result


@app.get("/api/workspace-session", response_model=WorkspaceSessionResult)
async def workspace_session_status(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    return get_workspace_session_result(db, context.active_workspace_id)


@app.get("/api/settings", response_model=AppSettings)
async def get_settings_route(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    return get_workspace_settings(db, context.active_workspace_id)


@app.post("/api/settings", response_model=AppSettings)
async def update_settings_route(
    payload: AppSettings,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    updated = update_workspace_settings(db, context.active_workspace_id, payload)
    record_audit_log(
        db,
        workspace_id=context.active_workspace_id,
        action="workspace.settings.update",
        entity_type="workspace_settings",
        entity_id=str(context.active_workspace_id),
        actor_type=AuditActorType.USER,
        actor_user_id=context.user.id,
        metadata={"proxy_enabled": updated.proxy_enabled},
    )
    return updated


@app.get("/api/contacts", response_model=ContactListResult)
async def get_contacts(
    search: str | None = None,
    unread_only: bool = False,
    identity_source: str = "all",
    sort_by: str = "name",
    sort_order: str = "asc",
    selected_ids: str | None = None,
    context: RequestContext = Depends(get_request_context),
    db: Session = Depends(get_db),
):
    filters = {
        "search": search,
        "unread_only": unread_only,
        "identity_source": identity_source,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "selected_ids": [item.strip() for item in (selected_ids or "").split(",") if item.strip()],
    }
    return list_contacts(db, context.active_workspace_id, filters=ContactQueryParams(**filters))


@app.post("/api/contacts/sync", response_model=JobSubmissionResult)
async def sync_contacts(
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    driver = await _workspace_driver(context.active_workspace_id)
    if await driver.is_login_browser_active():
        login_result = LoginStatus(**(await driver.check_login_status()))
        update_workspace_login_status(db, context.active_workspace_id, login_result)
        if login_result.state == LoginState.WAITING_QR:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Zalo login is still waiting for a QR scan. Finish or stop login before syncing contacts.",
            )
    try:
        return create_job(
            db,
            workspace_id=context.active_workspace_id,
            user_id=context.user.id,
            job_type=JobType.CONTACT_SYNC,
            payload={},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/contacts/sync-runs", response_model=ContactSyncRunListResult)
async def get_contact_sync_runs(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    return list_sync_runs(db, context.active_workspace_id)


@app.post("/api/campaigns", response_model=CampaignOperationResult)
async def create_campaign_route(
    payload: CampaignDraftPayload,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    if not payload.name.strip():
        raise HTTPException(400, "Campaign name is required.")
    if not payload.message.strip():
        raise HTTPException(400, "Campaign message cannot be empty.")
    if not payload.filters.selected_ids:
        raise HTTPException(400, "Select at least one campaign recipient before saving.")
    return create_campaign(db, context.active_workspace_id, context.user.id, payload)


@app.get("/api/campaigns", response_model=CampaignListResult)
async def list_campaigns_route(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    return list_campaigns(db, context.active_workspace_id)


@app.get("/api/campaigns/{campaign_id}/progress", response_model=CampaignProgressResult)
async def campaign_progress(campaign_id: int, context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    try:
        return get_campaign_progress(db, context.active_workspace_id, campaign_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/campaigns/{campaign_id}/execute", response_model=JobSubmissionResult)
async def execute_campaign(
    campaign_id: int,
    payload: CampaignExecutePayload,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    if payload.delay_min < 0 or payload.delay_max < 0:
        raise HTTPException(400, "Delay values must be non-negative.")
    if payload.delay_max < payload.delay_min:
        raise HTTPException(400, "Max delay must be greater than or equal to min delay.")
    try:
        campaign = get_campaign(db, context.active_workspace_id, campaign_id)
        submission = create_job(
            db,
            workspace_id=context.active_workspace_id,
            user_id=context.user.id,
            job_type=JobType.CAMPAIGN_SEND,
            payload={"campaign_id": campaign_id, "delay_min": payload.delay_min, "delay_max": payload.delay_max},
        )
        campaign.last_job_id = submission.job_id
        campaign.status = CampaignStatus.QUEUED
        return submission
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc))


@app.post("/api/message/send", response_model=JobSubmissionResult)
async def send_messages(
    payload: MessagePayload,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    if not payload.targets:
        raise HTTPException(400, "No targets provided.")
    if not payload.message.strip():
        raise HTTPException(400, "Message cannot be empty.")
    try:
        return create_job(
            db,
            workspace_id=context.active_workspace_id,
            user_id=context.user.id,
            job_type=JobType.MANUAL_SEND,
            payload=payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/friends/add", response_model=JobSubmissionResult)
async def add_friends(
    payload: FriendRequestPayload,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    if not payload.phone_numbers:
        raise HTTPException(400, "No phone numbers provided.")
    try:
        return create_job(
            db,
            workspace_id=context.active_workspace_id,
            user_id=context.user.id,
            job_type=JobType.FRIEND_REQUEST,
            payload=payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/groups/message", response_model=JobSubmissionResult)
async def group_message(
    payload: GroupMessagePayload,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    if not payload.group_name.strip():
        raise HTTPException(400, "Group name is required.")
    if not payload.message.strip():
        raise HTTPException(400, "Message cannot be empty.")
    try:
        return create_job(
            db,
            workspace_id=context.active_workspace_id,
            user_id=context.user.id,
            job_type=JobType.GROUP_SEND,
            payload=payload.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/jobs", response_model=JobListResult)
async def jobs_list(context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    return list_jobs(db, context.active_workspace_id)


@app.get("/api/jobs/{job_id}", response_model=JobResult)
async def job_detail(job_id: UUID, context: RequestContext = Depends(get_request_context), db: Session = Depends(get_db)):
    try:
        return get_job(db, context.active_workspace_id, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/jobs/{job_id}/cancel", response_model=CancelJobResult)
async def cancel_job_route(
    job_id: UUID,
    context: RequestContext = Depends(require_role(MembershipRole.OPERATOR)),
    db: Session = Depends(get_db),
):
    try:
        return cancel_job(db, context.active_workspace_id, job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
