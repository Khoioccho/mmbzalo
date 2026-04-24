"""
FastAPI application — MMBZalo Automation Tool
Full feature set: Login, Messaging, Friend Requests, Groups, Contacts, Settings.
"""

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.models import (
    LoginStatus,
    ContactListResult,
    MessagePayload,
    MessageResult,
    FriendRequestPayload,
    FriendRequestResult,
    GroupMessagePayload,
    GroupResult,
    AppSettings,
)
from app.zalo_driver import get_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("api")

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
async def get_contacts():
    """Sync and return the contact/conversation list."""
    driver = await get_driver()
    try:
        result = await driver.sync_contacts()
        return ContactListResult(**result)
    except Exception as e:
        logger.exception("Contact sync failed")
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
