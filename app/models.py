"""
Pydantic models for request/response schemas.
Covers: Login, Messaging, Friend Requests, Groups, Contacts, Settings.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


# ─── Enums ───────────────────────────────────────────────────────

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


# ─── Cookie Models (kept from Phase 1) ──────────────────────────

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


# ─── Login ───────────────────────────────────────────────────────

class LoginStatus(BaseModel):
    state: LoginState = LoginState.IDLE
    profile_name: Optional[str] = None
    profile_avatar: Optional[str] = None
    phone_number: Optional[str] = None
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─── Contacts ────────────────────────────────────────────────────

class ContactInfo(BaseModel):
    name: str
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    last_message: Optional[str] = None
    unread: bool = False


class ContactListResult(BaseModel):
    contacts: list[ContactInfo] = []
    contact_count: int = 0
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─── Messaging ───────────────────────────────────────────────────

class MessagePayload(BaseModel):
    """Send a message to one or more targets."""
    targets: list[str] = Field(
        ...,
        description="List of phone numbers or contact names to message."
    )
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
    results: list[MessageResultItem] = []
    state: TaskState = TaskState.COMPLETED
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─── Friend Requests ─────────────────────────────────────────────

class FriendRequestPayload(BaseModel):
    """Send friend requests via phone numbers."""
    phone_numbers: list[str] = Field(
        ...,
        description="List of phone numbers to send friend requests to."
    )
    greeting_message: Optional[str] = Field(
        None,
        description="Optional custom greeting message attached to the request."
    )
    exclude_admins: bool = Field(
        True,
        description="Exclude group admins when extracting from groups."
    )


class FriendRequestResultItem(BaseModel):
    phone: str
    success: bool
    error: Optional[str] = None


class FriendRequestResult(BaseModel):
    total: int = 0
    sent: int = 0
    failed: int = 0
    results: list[FriendRequestResultItem] = []
    state: TaskState = TaskState.COMPLETED
    message: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ─── Group Functions ─────────────────────────────────────────────

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


# ─── Settings ────────────────────────────────────────────────────

class AppSettings(BaseModel):
    language: str = Field("vi", description="'vi' or 'en'")
    theme: str = Field("dark", description="'dark' or 'light'")
    layout: str = Field("vertical", description="'vertical' or 'horizontal'")
    proxy_enabled: bool = False
    proxy_address: Optional[str] = None
    proxy_port: Optional[int] = None
    delay_min: float = 15.0
    delay_max: float = 30.0
