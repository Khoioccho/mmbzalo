"""
Zalo Driver — Playwright-based browser automation for chat.zalo.me

Uses playwright.sync_api running in a dedicated thread to avoid
the Windows asyncio ProactorEventLoop / NotImplementedError issue
with uvicorn.

Full feature set:
  - Persistent context login (QR code / phone)
  - Session state management
  - Contact list synchronization
  - Direct messaging to phone numbers / contact names
  - Friend request automation
  - Group messaging
"""

import asyncio
import functools
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page

from app.models import (
    LoginState,
    ContactInfo,
    MessageResultItem,
    FriendRequestResultItem,
)

logger = logging.getLogger("zalo_driver")

ZALO_CHAT_URL = "https://chat.zalo.me/"
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "user_data")
AUTH_STATE_DIR = os.path.join(BASE_DIR, "auth_state")

# Single-thread executor — all Playwright calls run here
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


def _run_in_thread(fn, *args, **kwargs):
    """Schedule a sync function to run in the Playwright thread."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))


class ZaloDriver:
    """
    Manages Playwright browser sessions for Zalo Web.
    All Playwright calls use sync_api in a dedicated thread.
    """

    def __init__(self):
        self._pw: Optional[Playwright] = None
        self._login_context: Optional[BrowserContext] = None
        self._login_page: Optional[Page] = None
        self._login_state: LoginState = LoginState.IDLE
        self._profile_name: Optional[str] = None
        self._profile_avatar: Optional[str] = None
        self._worker_browser: Optional[Browser] = None

    # ═══════════════════════════════════════════════════════════
    #  LIFECYCLE (sync, runs in thread)
    # ═══════════════════════════════════════════════════════════

    def _ensure_pw(self):
        if not self._pw:
            self._pw = sync_playwright().start()
            logger.info("Playwright started (sync_api, threaded).")

    def _shutdown_sync(self):
        self._close_login_sync()
        self._close_worker_sync()
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        logger.info("Playwright shut down.")

    async def shutdown(self):
        await _run_in_thread(self._shutdown_sync)

    def _close_login_sync(self):
        if self._login_page:
            try:
                self._login_page.close()
            except Exception:
                pass
            self._login_page = None
        if self._login_context:
            try:
                self._login_context.close()
            except Exception:
                pass
            self._login_context = None

    def _close_worker_sync(self):
        if self._worker_browser:
            try:
                self._worker_browser.close()
            except Exception:
                pass
            self._worker_browser = None

    # ═══════════════════════════════════════════════════════════
    #  LOGIN (visible browser)
    # ═══════════════════════════════════════════════════════════

    def _start_login_sync(self) -> dict:
        self._ensure_pw()
        self._close_login_sync()

        os.makedirs(USER_DATA_DIR, exist_ok=True)

        self._login_context = self._pw.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1280, "height": 800},
            locale="vi-VN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        self._login_page = (
            self._login_context.pages[0]
            if self._login_context.pages
            else self._login_context.new_page()
        )
        self._login_page.goto(ZALO_CHAT_URL, wait_until="domcontentloaded", timeout=30_000)

        self._login_state = LoginState.WAITING_QR
        self._profile_name = None
        self._profile_avatar = None

        logger.info("Login browser opened.")
        return self._status_dict("Browser opened — please log in via QR code or phone number.")

    async def start_login(self) -> dict:
        return await _run_in_thread(self._start_login_sync)

    def _check_login_sync(self) -> dict:
        if self._login_state == LoginState.AUTHENTICATED:
            return self._status_dict("Already authenticated.")

        if not self._login_page or self._login_page.is_closed():
            self._login_state = LoginState.IDLE
            return self._status_dict("No login browser is open.")

        try:
            is_auth = self._detect_auth(self._login_page)
            if is_auth:
                self._login_state = LoginState.AUTHENTICATED
                self._extract_profile(self._login_page)
                self._save_session()
                logger.info(f"Login successful — profile: {self._profile_name}")
                return self._status_dict("Authenticated successfully!")
            else:
                self._login_state = LoginState.WAITING_QR
                return self._status_dict("Waiting for QR scan or phone login...")
        except Exception as e:
            logger.warning(f"Login check error: {e}")
            self._login_state = LoginState.ERROR
            return self._status_dict(f"Error: {e}")

    async def check_login_status(self) -> dict:
        return await _run_in_thread(self._check_login_sync)

    def _stop_login_sync(self) -> dict:
        self._close_login_sync()
        self._login_state = LoginState.IDLE
        self._profile_name = None
        self._profile_avatar = None
        logger.info("Login browser closed.")
        return self._status_dict("Login browser closed.")

    async def stop_login(self) -> dict:
        return await _run_in_thread(self._stop_login_sync)

    def _status_dict(self, message: str) -> dict:
        return {
            "state": self._login_state.value,
            "profile_name": self._profile_name,
            "profile_avatar": self._profile_avatar,
            "phone_number": None,
            "message": message,
        }

    # ═══════════════════════════════════════════════════════════
    #  WORKER (headless)
    # ═══════════════════════════════════════════════════════════

    def _get_worker_context(self) -> BrowserContext:
        self._ensure_pw()
        state_path = os.path.join(AUTH_STATE_DIR, "session.json")

        if os.path.exists(state_path):
            if not self._worker_browser or not self._worker_browser.is_connected():
                self._worker_browser = self._pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
            return self._worker_browser.new_context(
                storage_state=state_path,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                locale="vi-VN",
            )

        if os.path.exists(USER_DATA_DIR):
            return self._pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900},
                locale="vi-VN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

        raise RuntimeError("No session available. Please log in first.")

    def _worker_page(self) -> tuple:
        """Returns (context, page) with an authenticated Zalo session."""
        context = self._get_worker_context()
        page = context.pages[0] if hasattr(context, "pages") and context.pages else context.new_page()
        page.goto(ZALO_CHAT_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(3)

        if not self._detect_auth(page):
            context.close()
            raise RuntimeError("Session expired or invalid. Please log in again.")

        return context, page

    # ═══════════════════════════════════════════════════════════
    #  CONTACTS
    # ═══════════════════════════════════════════════════════════

    def _sync_contacts_sync(self) -> dict:
        context, page = self._worker_page()
        try:
            # Try clicking contacts tab
            for sel in ['[data-tab="contacts"]', '[class*="contact"]', '[class*="phonebook"]',
                        'div[title*="Danh b"]', 'div[title*="Contact"]']:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        el.click()
                        time.sleep(2)
                        logger.info(f"Clicked contacts tab: {sel}")
                        break
                except Exception:
                    continue

            contacts_data = page.evaluate("""
                () => {
                    const results = []; const seen = new Set();
                    const sels = ['div[data-id]','div[class*="conv-item"]','div[class*="ConversationItem"]',
                                  'div[class*="friend-item"]','div[class*="contact-item"]',
                                  '[role="listitem"]','[role="option"]'];
                    for (const s of sels) {
                        for (const el of document.querySelectorAll(s)) {
                            const nameEl = el.querySelector('span,p,[class*="name"],[class*="truncate"]');
                            const msgEl  = el.querySelector('[class*="msg"],[class*="last-msg"],[class*="subtitle"]');
                            const imgEl  = el.querySelector('img');
                            const badge  = el.querySelector('[class*="badge"],[class*="unread"]');
                            const name = nameEl ? nameEl.textContent.trim() : '';
                            if (name && !seen.has(name) && name.length < 100) {
                                seen.add(name);
                                results.push({ name, avatar_url: imgEl?.src||null,
                                    last_message: msgEl?.textContent.trim()||null, unread: !!badge });
                            }
                        }
                        if (results.length > 0) break;
                    }
                    if (results.length === 0) {
                        for (const el of document.querySelectorAll('div[tabindex="0"]')) {
                            const text = el.textContent.trim(); const imgEl = el.querySelector('img');
                            if (text && imgEl && text.length < 300) {
                                const name = text.split('\\n')[0].trim();
                                if (name && !seen.has(name)) { seen.add(name);
                                    results.push({ name, avatar_url: imgEl.src, last_message: null, unread: false }); }
                            }
                        }
                    }
                    return results;
                }
            """)

            contacts = [ContactInfo(**c) for c in contacts_data]
            return {"contacts": contacts, "contact_count": len(contacts),
                    "message": f"Synced {len(contacts)} contact(s)."}
        finally:
            context.close()

    async def sync_contacts(self) -> dict:
        return await _run_in_thread(self._sync_contacts_sync)

    # ═══════════════════════════════════════════════════════════
    #  MESSAGING
    # ═══════════════════════════════════════════════════════════

    def _send_messages_sync(self, targets, message, delay_min, delay_max) -> dict:
        context, page = self._worker_page()
        results = []
        try:
            for i, target in enumerate(targets):
                logger.info(f"Messaging {i+1}/{len(targets)}: {target}")
                success, error = False, None
                try:
                    if not self._open_search(page):
                        raise Exception("Could not open search bar.")
                    page.keyboard.type(target, delay=50)
                    time.sleep(2)
                    if not self._click_search_result(page):
                        raise Exception(f"No result for '{target}'.")
                    time.sleep(1)
                    if not self._type_and_send(page, message):
                        raise Exception("Could not send message.")
                    success = True
                    logger.info(f"Message sent to {target}")
                except Exception as e:
                    error = str(e)
                    logger.warning(f"Failed: {target}: {e}")

                results.append(MessageResultItem(target=target, success=success, error=error))
                if i < len(targets) - 1:
                    time.sleep(random.uniform(delay_min, delay_max))

            sent = sum(1 for r in results if r.success)
            failed = len(results) - sent
            return {"total": len(targets), "sent": sent, "failed": failed,
                    "results": results, "message": f"Sent {sent}/{len(targets)} ({failed} failed)."}
        finally:
            context.close()

    async def send_messages(self, targets, message, delay_min=15.0, delay_max=30.0) -> dict:
        return await _run_in_thread(self._send_messages_sync, targets, message, delay_min, delay_max)

    # ═══════════════════════════════════════════════════════════
    #  FRIEND REQUESTS
    # ═══════════════════════════════════════════════════════════

    def _send_friend_requests_sync(self, phone_numbers, greeting_message) -> dict:
        context, page = self._worker_page()
        results = []
        try:
            for i, phone in enumerate(phone_numbers):
                logger.info(f"Friend request {i+1}/{len(phone_numbers)}: {phone}")
                success, error = False, None
                try:
                    if not self._open_search(page):
                        raise Exception("Could not open search bar.")
                    page.keyboard.type(phone, delay=50)
                    time.sleep(2)

                    add_sels = ['button:has-text("K\\u1EBFt b\\u1EA1n")', 'button:has-text("Add friend")',
                                'button:has-text("Add Friend")', '[class*="add-friend"]', '[class*="AddFriend"]']
                    added = False
                    for sel in add_sels:
                        try:
                            btn = page.locator(sel).first
                            if btn.count() > 0:
                                if greeting_message:
                                    try:
                                        inp = page.locator('textarea, input[type="text"]').last
                                        if inp.count() > 0:
                                            inp.fill(greeting_message)
                                    except Exception:
                                        pass
                                btn.click()
                                time.sleep(1)
                                added = True
                                break
                        except Exception:
                            continue

                    if not added:
                        if self._click_search_result(page):
                            time.sleep(1)
                            for sel in add_sels:
                                try:
                                    btn = page.locator(sel).first
                                    if btn.count() > 0:
                                        btn.click()
                                        added = True
                                        break
                                except Exception:
                                    continue

                    if added:
                        success = True
                    else:
                        raise Exception(f"No 'Add Friend' button for {phone}.")

                except Exception as e:
                    error = str(e)
                    logger.warning(f"Failed: {phone}: {e}")

                results.append(FriendRequestResultItem(phone=phone, success=success, error=error))
                if i < len(phone_numbers) - 1:
                    time.sleep(random.uniform(10, 20))

            sent = sum(1 for r in results if r.success)
            failed = len(results) - sent
            return {"total": len(phone_numbers), "sent": sent, "failed": failed,
                    "results": results, "message": f"Sent {sent}/{len(phone_numbers)} ({failed} failed)."}
        finally:
            context.close()

    async def send_friend_requests(self, phone_numbers, greeting_message=None) -> dict:
        return await _run_in_thread(self._send_friend_requests_sync, phone_numbers, greeting_message)

    # ═══════════════════════════════════════════════════════════
    #  GROUPS
    # ═══════════════════════════════════════════════════════════

    def _send_group_message_sync(self, group_name, message) -> dict:
        context, page = self._worker_page()
        try:
            if not self._open_search(page):
                raise RuntimeError("Could not open search bar.")
            page.keyboard.type(group_name, delay=50)
            time.sleep(2)
            if not self._click_search_result(page):
                raise RuntimeError(f"Group '{group_name}' not found.")
            time.sleep(1)
            if not self._type_and_send(page, message):
                raise RuntimeError("Could not send message in group.")
            logger.info(f"Message sent to group '{group_name}'")
            return {"success": True, "group_name": group_name,
                    "message": f"Message sent to group '{group_name}'."}
        except Exception as e:
            logger.warning(f"Group message failed: {e}")
            return {"success": False, "group_name": group_name, "message": str(e)}
        finally:
            context.close()

    async def send_group_message(self, group_name, message) -> dict:
        return await _run_in_thread(self._send_group_message_sync, group_name, message)

    # ═══════════════════════════════════════════════════════════
    #  HELPERS (all sync, run in thread)
    # ═══════════════════════════════════════════════════════════

    def _detect_auth(self, page: Page) -> bool:
        try:
            url = page.url
            if "login" in url.lower():
                return False
            for sel in ["#contact-list-container", "[class*='sidebar']", "[class*='conv-list']",
                        "[class*='chat-list']", "[data-id]", "#lst-conversation",
                        "div[class*='Conversation']", "div[class*='NavBar']"]:
                try:
                    if page.locator(sel).count() > 0:
                        return True
                except Exception:
                    continue
            if page.locator("div[tabindex]").count() > 3:
                return True
            return False
        except Exception:
            return False

    def _extract_profile(self, page: Page):
        try:
            for sel in ["[class*='avatar'] + span", "[class*='user-name']",
                        "[class*='profile-name']", "#main-menu .user-name"]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        self._profile_name = el.inner_text()
                        break
                except Exception:
                    continue
        except Exception:
            pass
        try:
            img = page.locator("img[class*='avatar'], img[class*='Avatar']").first
            if img.count() > 0:
                self._profile_avatar = img.get_attribute("src")
        except Exception:
            pass

    def _save_session(self):
        if not self._login_context:
            return
        try:
            os.makedirs(AUTH_STATE_DIR, exist_ok=True)
            path = os.path.join(AUTH_STATE_DIR, "session.json")
            storage = self._login_context.storage_state()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(storage, f, indent=2)
            logger.info(f"Session saved to {path}")
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def _open_search(self, page: Page) -> bool:
        for sel in ['input[placeholder*="T\\u00ECm ki\\u1EBFm"]', 'input[placeholder*="Search"]',
                    'input[type="search"]', '[class*="search"] input', '[class*="Search"] input',
                    '#contact-search-input']:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    el.fill("")
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
        try:
            icon = page.locator('[class*="search"] svg, [class*="Search"] svg, [class*="ic-search"]').first
            if icon.count() > 0:
                icon.click()
                time.sleep(0.5)
                return True
        except Exception:
            pass
        return False

    def _click_search_result(self, page: Page) -> bool:
        for sel in ['[class*="search-result"] > div:first-child', '[class*="SearchResult"] > div:first-child',
                    '[class*="search-item"]:first-child', '[class*="SearchItem"]:first-child',
                    '[role="listbox"] [role="option"]:first-child']:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    return True
            except Exception:
                continue
        try:
            items = page.locator('div[data-id]')
            if items.count() > 0:
                items.first.click()
                return True
        except Exception:
            pass
        return False

    def _type_and_send(self, page: Page, message: str) -> bool:
        for sel in ['[data-testid="message-input"]', 'div[contenteditable="true"]',
                    '#chatTextInput', '[class*="chat-input"] div[contenteditable]',
                    '[class*="ChatInput"] div[contenteditable]', 'div[role="textbox"]']:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    el.click()
                    el.fill("")
                    page.keyboard.type(message, delay=30)
                    time.sleep(0.5)
                    page.keyboard.press("Enter")
                    time.sleep(1)
                    return True
            except Exception:
                continue
        return False


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_driver: Optional[ZaloDriver] = None


async def get_driver() -> ZaloDriver:
    global _driver
    if _driver is None:
        _driver = ZaloDriver()
    return _driver
