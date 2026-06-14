from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.db_models import CampaignStatus, JobFailureClass, JobStatus, JobType, MembershipRole, WorkspaceLoginState
from app.models import (
    AppSettings,
    CampaignDraftPayload,
    CampaignInfo,
    CampaignProgressEvent,
    CampaignProgressResult,
    ContactInfo,
    ContactQueryParams,
    ContactSyncRunInfo,
    FriendRequestPayload,
    GroupMessagePayload,
    LoginStatus,
    MessagePayload,
)


class LoginRequest(BaseModel):
    email: str
    password: str


class WorkspaceSummary(BaseModel):
    workspace_id: UUID
    slug: str
    name: str
    role: MembershipRole
    login_state: WorkspaceLoginState = WorkspaceLoginState.IDLE


class UserSummary(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    is_platform_admin: bool = False


class AuthSessionResult(BaseModel):
    user: UserSummary
    active_workspace_id: UUID | None = None
    workspaces: list[WorkspaceSummary] = Field(default_factory=list)


class WorkspaceSwitchResult(BaseModel):
    active_workspace_id: UUID
    workspace: WorkspaceSummary


class JobSubmissionResult(BaseModel):
    job_id: UUID
    workspace_id: UUID
    status: JobStatus
    type: JobType
    message: str = ""
    created_at: datetime


class JobEventResult(BaseModel):
    sequence_no: int
    level: str
    event_type: str
    message: str
    target: str | None = None
    route: str | None = None
    success: bool | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class JobResult(BaseModel):
    job_id: UUID
    workspace_id: UUID
    type: JobType
    status: JobStatus
    failure_class: JobFailureClass
    cancel_requested: bool = False
    attempt_count: int = 0
    max_attempts: int = 0
    payload: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    events: list[JobEventResult] = Field(default_factory=list)


class JobListResult(BaseModel):
    jobs: list[JobResult] = Field(default_factory=list)
    total: int = 0


class CancelJobResult(BaseModel):
    job_id: UUID
    status: JobStatus
    cancel_requested: bool


class ManualSendJobPayload(MessagePayload):
    pass


class GroupSendJobPayload(GroupMessagePayload):
    pass


class FriendRequestJobPayload(FriendRequestPayload):
    pass


class WorkspaceSessionResult(BaseModel):
    workspace_id: UUID
    owner_worker_id: str
    login_state: WorkspaceLoginState
    profile_path: str
    profile_name: str | None = None
    profile_avatar_url: str | None = None
    phone_number: str | None = None
    last_authenticated_at: datetime | None = None
    last_validated_at: datetime | None = None
    error_message: str | None = None


class ReadinessResult(BaseModel):
    status: str
    database: str
    worker: str
    host_identity: str


class LegacyImportResult(BaseModel):
    workspace_id: UUID
    imported_contacts: int
    imported_sync_runs: int
    imported_campaigns: int
    imported_settings: bool


class BootstrapAdminResult(BaseModel):
    user_id: UUID
    workspace_id: UUID
    email: str
    workspace_slug: str
