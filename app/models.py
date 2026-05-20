"""
Pydantic models for request/response schemas.
Covers: Login, Messaging, Friend Requests, Groups, Contacts, Campaigns, Settings.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class LoginState(str, Enum):
    IDLE = "idle"
    WAITING_QR = "waiting_qr"
    AUTHENTICATED = "authenticated"
    EXPIRED = "expired"
    ERROR = "error"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CookieItem(BaseModel):
    """Single browser cookie as extracted from DevTools."""

    name: str
    value: str
    domain: str = ".zalo.me"
    path: str = "/"
    secure: bool = True
    httpOnly: bool = False
    sameSite: Optional[str] = "Lax"
    expires: Optional[float] = None


class LoginStatus(BaseModel):
    state: LoginState = LoginState.IDLE
    profile_name: Optional[str] = None
    profile_avatar: Optional[str] = None
    phone_number: Optional[str] = None
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ContactInfo(BaseModel):
    name: str
    zid: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    last_message: Optional[str] = None
    unread: bool = False
    identity_key: Optional[str] = None
    identity_source: Optional[str] = None
    last_seen_at: Optional[str] = None


class ContactSyncDiagnostics(BaseModel):
    login_detected: bool = False
    target_view_detected: bool = False
    selector_family: Optional[str] = None
    elapsed_seconds: float = 0.0
    total_passes: int = 0
    scroll_passes: int = 0
    forward_passes: int = 0
    verification_passes: int = 0
    raw_nodes_found: int = 0
    deduplicated_contacts: int = 0
    unique_ids_found: int = 0
    contacts_without_ids: int = 0
    bottom_reached: bool = False
    verification_stabilized: bool = False
    ended_by_timeout: bool = False
    ended_by_safety_limit: bool = False
    empty_state_detected: bool = False
    debug_artifacts: list[str] = Field(default_factory=list)


class ContactListResult(BaseModel):
    contacts: list[ContactInfo] = Field(default_factory=list)
    contact_count: int = 0
    stored_contact_count: int = 0
    sync_status: str = "unknown"
    sync_run_id: Optional[int] = None
    last_sync_at: Optional[str] = None
    last_sync_status: Optional[str] = None
    diagnostics: ContactSyncDiagnostics = Field(default_factory=ContactSyncDiagnostics)
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class ContactQueryParams(BaseModel):
    search: Optional[str] = None
    unread_only: bool = False
    identity_source: str = "all"
    sort_by: str = "name"
    sort_order: str = "asc"
    selected_ids: list[str] = Field(default_factory=list)


class ContactSyncRunInfo(BaseModel):
    sync_run_id: int
    sync_status: str
    contact_count: int = 0
    stored_contact_count: int = 0
    message: str = ""
    timestamp: str
    diagnostics: ContactSyncDiagnostics = Field(default_factory=ContactSyncDiagnostics)


class ContactSyncRunListResult(BaseModel):
    runs: list[ContactSyncRunInfo] = Field(default_factory=list)
    total: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class CampaignContactPreview(BaseModel):
    identity_key: str
    name: str
    avatar_url: Optional[str] = None
    unread: bool = False
    identity_source: Optional[str] = None
    last_seen_at: Optional[str] = None


class CampaignDraftPayload(BaseModel):
    name: str = Field(..., description="Campaign name.")
    message: str = Field(..., description="Drafted message content.")
    filters: ContactQueryParams = Field(default_factory=ContactQueryParams)


class CampaignExecutePayload(BaseModel):
    delay_min: float = Field(15.0, description="Min delay between sends (seconds).")
    delay_max: float = Field(30.0, description="Max delay between sends (seconds).")


class CampaignResultItem(BaseModel):
    identity_key: str
    target: str
    name: str
    success: bool
    error: Optional[str] = None


class CampaignInfo(BaseModel):
    campaign_id: int
    name: str
    message: str
    filters: ContactQueryParams = Field(default_factory=ContactQueryParams)
    selected_contact_ids: list[str] = Field(default_factory=list)
    matched_contacts: list[CampaignContactPreview] = Field(default_factory=list)
    matched_count: int = 0
    status: str = "draft"
    sent_count: int = 0
    failed_count: int = 0
    results: list[CampaignResultItem] = Field(default_factory=list)
    created_at: str
    executed_at: Optional[str] = None


class CampaignListResult(BaseModel):
    campaigns: list[CampaignInfo] = Field(default_factory=list)
    total: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class CampaignOperationResult(BaseModel):
    campaign: CampaignInfo
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class MessagePayload(BaseModel):
    """Send a message to one or more targets."""

    targets: list[str] = Field(..., description="List of phone numbers or contact names to message.")
    message: str = Field(..., description="Message content to send.")
    delay_min: float = Field(15.0, description="Min delay between sends (seconds).")
    delay_max: float = Field(30.0, description="Max delay between sends (seconds).")


class MessageResultItem(BaseModel):
    target: str
    success: bool
    error: Optional[str] = None


class MessageResult(BaseModel):
    total: int = 0
    sent: int = 0
    failed: int = 0
    results: list[MessageResultItem] = Field(default_factory=list)
    state: TaskState = TaskState.COMPLETED
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class FriendRequestPayload(BaseModel):
    """Send friend requests via phone numbers."""

    phone_numbers: list[str] = Field(..., description="List of phone numbers to send friend requests to.")
    greeting_message: Optional[str] = Field(
        None,
        description="Optional custom greeting message attached to the request.",
    )
    exclude_admins: bool = Field(
        True,
        description="Exclude group admins when extracting from groups.",
    )


class FriendRequestResultItem(BaseModel):
    phone: str
    success: bool
    error: Optional[str] = None


class FriendRequestResult(BaseModel):
    total: int = 0
    sent: int = 0
    failed: int = 0
    results: list[FriendRequestResultItem] = Field(default_factory=list)
    state: TaskState = TaskState.COMPLETED
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class GroupMessagePayload(BaseModel):
    """Send a message inside a Zalo group."""

    group_name: str = Field(..., description="Name of the target group.")
    message: str = Field(..., description="Message content.")


class GroupInvitePayload(BaseModel):
    """Invite phone numbers to a group."""

    group_name: str
    phone_numbers: list[str]


class GroupResult(BaseModel):
    success: bool
    group_name: str = ""
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class AppSettings(BaseModel):
    language: str = Field("vi", description="'vi' or 'en'")
    theme: str = Field("dark", description="'dark' or 'light'")
    layout: str = Field("vertical", description="'vertical' or 'horizontal'")
    proxy_enabled: bool = False
    proxy_address: Optional[str] = None
    proxy_port: Optional[int] = None
    delay_min: float = 15.0
    delay_max: float = 30.0
