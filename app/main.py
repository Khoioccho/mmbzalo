"""
FastAPI application — MMBZalo Automation Tool
Full feature set: Login, Messaging, Friend Requests, Groups, Contacts, Settings.
"""

import json
import logging
import os
import threading
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.models import (
    CampaignDraftPayload,
    CampaignExecutePayload,
    CampaignListResult,
    CampaignOperationResult,
    CampaignProgressResult,
    ContactQueryParams,
    LoginStatus,
    ContactListResult,
    ContactSyncRunListResult,
    MessagePayload,
    MessageResult,
    FriendRequestPayload,
    FriendRequestResult,
    GroupMessagePayload,
    GroupResult,
    AppSettings,
)
from app.contact_store import contact_store
from app.zalo_driver import get_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("api")

_campaign_progress_lock = threading.Lock()
_campaign_progress: dict[int, dict] = {}


def _reset_campaign_progress(campaign_id: int, total: int) -> None:
    with _campaign_progress_lock:
        _campaign_progress[campaign_id] = {
            "campaign_id": campaign_id,
            "status": "running",
            "total": total,
            "sent": 0,
            "failed": 0,
            "current": None,
            "sequence": 0,
            "events": [],
        }


def _record_campaign_progress(campaign_id: int, event: dict) -> None:
    with _campaign_progress_lock:
        state = _campaign_progress.setdefault(
            campaign_id,
            {
                "campaign_id": campaign_id,
                "status": "running",
                "total": 0,
                "sent": 0,
                "failed": 0,
                "current": None,
                "sequence": 0,
                "events": [],
            },
        )
        state["sequence"] += 1
        event_type = event.get("event")
        target = event.get("target")
        if event_type == "target_start":
            state["current"] = target
        elif event_type == "target_done":
            if event.get("success"):
                state["sent"] += 1
            else:
                state["failed"] += 1
        elif event_type == "complete":
            state["status"] = "completed"
            state["current"] = None

        state["events"].append(
            {
                "sequence": state["sequence"],
                "timestamp": datetime.utcnow().isoformat(),
                "level": event.get("level", "info"),
                "message": event.get("message", ""),
                "target": target,
                "route": event.get("route"),
                "success": event.get("success"),
            }
        )
        state["events"] = state["events"][-200:]


def _fail_campaign_progress(campaign_id: int, message: str) -> None:
    with _campaign_progress_lock:
        state = _campaign_progress.setdefault(
            campaign_id,
            {
                "campaign_id": campaign_id,
                "status": "failed",
                "total": 0,
                "sent": 0,
                "failed": 0,
                "current": None,
                "sequence": 0,
                "events": [],
            },
        )
        state["status"] = "failed"
    _record_campaign_progress(campaign_id, {"event": "failed", "level": "error", "message": message})


def _get_campaign_progress_snapshot(campaign_id: int) -> dict:
    with _campaign_progress_lock:
        state = _campaign_progress.get(campaign_id)
        if not state:
            return {"campaign_id": campaign_id, "status": "idle", "events": []}
        return {
            "campaign_id": state["campaign_id"],
            "status": state["status"],
            "total": state["total"],
            "sent": state["sent"],
            "failed": state["failed"],
            "current": state["current"],
            "events": list(state["events"]),
        }

# ─── Settings file path ─────────────────────────────────────────
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "settings.json")

_settings = AppSettings()


def _load_settings():
    global _settings
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                _settings = AppSettings(**json.load(f))
        except Exception:
            _settings = AppSettings()


def _save_settings():
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(_settings.model_dump(), f, indent=2)


# ─── App lifecycle ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_settings()
    contact_store.initialize()
    logger.info("MMBZalo Automation Tool started.")
    yield
    driver = await get_driver()
    await driver.shutdown()
    logger.info("Shut down complete.")


app = FastAPI(
    title="MMBZalo Automation Tool",
    description="Zalo Automation — Login, Messaging, Friends, Groups",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ═════════════════════════════════════════════════════════════════
#  PAGES
# ═════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


# ═════════════════════════════════════════════════════════════════
#  LOGIN
# ═════════════════════════════════════════════════════════════════

@app.post("/api/login/start", response_model=LoginStatus)
async def login_start():
    """Open a visible Chromium window for Zalo QR/phone login."""
    driver = await get_driver()
    try:
        result = await driver.start_login()
        return LoginStatus(**result)
    except Exception as e:
        logger.exception("Login start failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/login/status", response_model=LoginStatus)
async def login_status():
    """Check current login state."""
    driver = await get_driver()
    try:
        result = await driver.check_login_status()
        return LoginStatus(**result)
    except Exception as e:
        logger.exception("Login status check failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/login/stop", response_model=LoginStatus)
async def login_stop():
    """Close the login browser."""
    driver = await get_driver()
    try:
        result = await driver.stop_login()
        return LoginStatus(**result)
    except Exception as e:
        logger.exception("Login stop failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════════
#  CONTACTS
# ═════════════════════════════════════════════════════════════════

@app.get("/api/contacts", response_model=ContactListResult)
async def get_contacts(
    search: str | None = None,
    unread_only: bool = False,
    identity_source: str = "all",
    sort_by: str = "name",
    sort_order: str = "asc",
    selected_ids: str | None = None,
):
    """Return the latest persisted contact list and sync metadata."""
    try:
        filters = ContactQueryParams(
            search=search,
            unread_only=unread_only,
            identity_source=identity_source,
            sort_by=sort_by,
            sort_order=sort_order,
            selected_ids=[item.strip() for item in (selected_ids or "").split(",") if item.strip()],
        )
        return contact_store.get_contacts_result(filters)
    except Exception as e:
        logger.exception("Loading stored contacts failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contacts/sync", response_model=ContactListResult)
async def sync_contacts():
    """Run a live Zalo sync and persist the result locally."""
    driver = await get_driver()
    try:
        result = await driver.sync_contacts()
        persisted = contact_store.persist_sync_result(result)
        return ContactListResult(**persisted.model_dump())
    except Exception as e:
        logger.exception("Contact sync failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/contacts/sync-runs", response_model=ContactSyncRunListResult)
async def get_contact_sync_runs():
    """Return recent persisted contact sync runs."""
    try:
        runs = contact_store.list_sync_runs()
        return ContactSyncRunListResult(runs=runs, total=len(runs))
    except Exception as e:
        logger.exception("Loading contact sync history failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/campaigns", response_model=CampaignOperationResult)
async def create_campaign(payload: CampaignDraftPayload):
    """Create a local campaign draft from stored contacts."""
    if not payload.name.strip():
        raise HTTPException(400, "Campaign name is required.")
    if not payload.message.strip():
        raise HTTPException(400, "Campaign message cannot be empty.")
    if not payload.filters.selected_ids:
        raise HTTPException(400, "Select at least one campaign recipient before saving.")
    try:
        campaign = contact_store.create_campaign(payload)
        return CampaignOperationResult(
            campaign=campaign,
            message=f"Campaign '{campaign.name}' saved with {campaign.matched_count} matched contact(s).",
        )
    except Exception as e:
        logger.exception("Campaign creation failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/campaigns", response_model=CampaignListResult)
async def list_campaigns():
    """Return recent saved campaigns."""
    try:
        campaigns = contact_store.list_campaigns()
        return CampaignListResult(campaigns=campaigns, total=len(campaigns))
    except Exception as e:
        logger.exception("Campaign list failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/campaigns/{campaign_id}/progress", response_model=CampaignProgressResult)
async def get_campaign_progress(campaign_id: int):
    """Return live progress for a running or recently completed campaign."""
    return CampaignProgressResult(**_get_campaign_progress_snapshot(campaign_id))


@app.post("/api/campaigns/{campaign_id}/execute", response_model=CampaignOperationResult)
async def execute_campaign(campaign_id: int, payload: CampaignExecutePayload):
    """Execute a saved campaign through the existing messaging driver."""
    if payload.delay_min < 0 or payload.delay_max < 0:
        raise HTTPException(400, "Delay values must be non-negative.")
    if payload.delay_max < payload.delay_min:
        raise HTTPException(400, "Max delay must be greater than or equal to min delay.")

    driver = await get_driver()
    try:
        prepared = contact_store.prepare_campaign_execution(campaign_id)
        campaign = prepared["campaign"]
        matched_contacts = prepared["matched_contacts"]
        if not matched_contacts:
            raise HTTPException(400, "Campaign has no matched contacts to execute.")
        _reset_campaign_progress(campaign_id, len(matched_contacts))
        send_result = await driver.send_campaign_messages(
            contacts=matched_contacts,
            message=campaign.message,
            delay_min=payload.delay_min,
            delay_max=payload.delay_max,
            progress_callback=lambda event: _record_campaign_progress(campaign_id, event),
        )
        updated_campaign = contact_store.finalize_campaign_execution(
            campaign_id=campaign_id,
            matched_contacts=campaign.matched_contacts,
            send_result=send_result,
        )
        return CampaignOperationResult(
            campaign=updated_campaign,
            message=f"Campaign '{updated_campaign.name}' executed: {updated_campaign.sent_count} sent, {updated_campaign.failed_count} failed.",
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.exception("Campaign execution failed")
        _fail_campaign_progress(campaign_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════════
#  MESSAGING
# ═════════════════════════════════════════════════════════════════

@app.post("/api/message/send", response_model=MessageResult)
async def send_messages(payload: MessagePayload):
    """Send a message to a list of phone numbers or contact names."""
    if not payload.targets:
        raise HTTPException(400, "No targets provided.")
    if not payload.message.strip():
        raise HTTPException(400, "Message cannot be empty.")

    driver = await get_driver()
    try:
        result = await driver.send_messages(
            targets=payload.targets,
            message=payload.message,
            delay_min=payload.delay_min,
            delay_max=payload.delay_max,
        )
        return MessageResult(**result)
    except Exception as e:
        logger.exception("Messaging failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════════
#  FRIEND REQUESTS
# ═════════════════════════════════════════════════════════════════

@app.post("/api/friends/add", response_model=FriendRequestResult)
async def add_friends(payload: FriendRequestPayload):
    """Send friend requests to a list of phone numbers."""
    if not payload.phone_numbers:
        raise HTTPException(400, "No phone numbers provided.")

    driver = await get_driver()
    try:
        result = await driver.send_friend_requests(
            phone_numbers=payload.phone_numbers,
            greeting_message=payload.greeting_message,
        )
        return FriendRequestResult(**result)
    except Exception as e:
        logger.exception("Friend requests failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════════
#  GROUPS
# ═════════════════════════════════════════════════════════════════

@app.post("/api/groups/message", response_model=GroupResult)
async def group_message(payload: GroupMessagePayload):
    """Send a message in a Zalo group."""
    if not payload.group_name.strip():
        raise HTTPException(400, "Group name is required.")
    if not payload.message.strip():
        raise HTTPException(400, "Message cannot be empty.")

    driver = await get_driver()
    try:
        result = await driver.send_group_message(
            group_name=payload.group_name,
            message=payload.message,
        )
        return GroupResult(**result)
    except Exception as e:
        logger.exception("Group message failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═════════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════════

@app.get("/api/settings", response_model=AppSettings)
async def get_settings():
    return _settings


@app.post("/api/settings", response_model=AppSettings)
async def update_settings(payload: AppSettings):
    global _settings
    _settings = payload
    _save_settings()
    return _settings
