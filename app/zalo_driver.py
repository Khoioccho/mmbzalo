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
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext, Page

from app.contact_name_utils import choose_best_contact_name, normalize_contact_name
from app.models import (
    CampaignContactPreview,
    LoginState,
    ContactInfo,
    ContactSyncDiagnostics,
    MessageResultItem,
    FriendRequestResultItem,
)

logger = logging.getLogger("zalo_driver")

ZALO_CHAT_URL = "https://chat.zalo.me/"
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "user_data")
AUTH_STATE_DIR = os.path.join(BASE_DIR, "auth_state")
SYNC_DEBUG_DIR = os.path.join(BASE_DIR, "debug_sync")
SYNC_DEBUG_ENABLED = os.getenv("ZALO_SYNC_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
SEND_DEBUG_ENABLED = (
    os.getenv("ZALO_SEND_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
    or SYNC_DEBUG_ENABLED
)

CONTACT_VIEW_SELECTORS = [
    {
        "name": "primary_virtualized",
        "nav": [
            '#main-tab .leftbar-tab.selected[title*="Danh b"]',
            '#main-tab .leftbar-tab[title*="Danh b"]',
            '#main-tab .leftbar-tab[title*="Contact"]',
            '[data-tab="contacts"]',
        ],
        "view_markers": [
            'text=/danh bạ|contacts/i',
            '[aria-label*="Danh b"]',
            '[aria-label*="Contact"]',
            '[title*="Danh b"]',
            '[title*="Contact"]',
        ],
        "container": [
            '.ReactVirtualized__Grid',
            '.ReactVirtualized__List',
            '[class*="contact-list"]',
            '[class*="ContactList"]',
            '[class*="friend-list"]',
            '[class*="FriendList"]',
        ],
        "item": [
            'div[data-id]',
            '[role="listitem"]',
            '[role="option"]',
            'div[class*="friend-item"]',
            'div[class*="contact-item"]',
        ],
    },
    {
        "name": "fallback_sidebar",
        "nav": [
            '[class*="contact"]',
            '[class*="phonebook"]',
            '[class*="sidebar"] [role="button"]',
        ],
        "view_markers": [
            'text=/danh bạ|contacts/i',
            '[class*="contact"]',
            '[class*="phonebook"]',
        ],
        "container": [
            '[class*="sidebar"]',
            '[class*="scroll"]',
            '[id*="scroll"]',
            '[role="listbox"]',
            '[class*="list"]',
        ],
        "item": [
            'div[data-id]',
            'div[tabindex="0"]',
            '[role="listitem"]',
            '[role="option"]',
        ],
    },
]

EMPTY_STATE_SELECTORS = [
    'text=/không có liên hệ|chưa có liên hệ|không có cuộc trò chuyện|no contacts|no conversations/i',
    '[class*="empty"]',
    '[class*="Empty"]',
]

CONVERSATION_VIEW_MARKERS = [
    'text=/trò chuyện|messages|tin nhắn/i',
    '[class*="conversation"]',
    '[class*="chat-list"]',
    '[class*="ConversationList"]',
]

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
            import sys
            import asyncio
            if sys.platform == "win32":
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                logger.info("Set WindowsProactorEventLoopPolicy for Playwright thread.")
            
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
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled", 
                "--no-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process,ImprovedCookieControls",
                "--disable-site-isolation-trials"
            ],
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

        if os.path.exists(USER_DATA_DIR):
            return self._pw.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                channel="chrome",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled", 
                    "--no-sandbox",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process,ImprovedCookieControls",
                    "--disable-site-isolation-trials"
                ],
                viewport={"width": 1440, "height": 900},
                locale="vi-VN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

        raise RuntimeError("No session profile available. Please log in first.")

    def _worker_page(self) -> tuple:
        """Returns (context, page) with an authenticated Zalo session."""
        if self._login_context and self._login_page and not self._login_page.is_closed():
            self._wait_for_shell_ready(self._login_page)
            if self._detect_auth(self._login_page):
                logger.info("Reusing authenticated login browser for worker actions.")
                return self._login_context, self._login_page

        context = self._get_worker_context()
        page = context.pages[0] if hasattr(context, "pages") and context.pages else context.new_page()
        page.goto(ZALO_CHAT_URL, wait_until="domcontentloaded", timeout=60_000)

        self._wait_for_shell_ready(page)

        if not self._detect_auth(page):
            context.close()
            raise RuntimeError("Session expired or invalid. Please log in again.")

        return context, page

    def _wait_for_shell_ready(self, page: Page, timeout_ms: int = 45_000):
        """Wait until the Zalo shell has rendered enough UI for navigation."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            logger.info("Network idle wait timed out; falling back to DOM probes.")

        deadline = time.time() + max(timeout_ms / 1000, 5)
        shell_selectors = [
            '[class*="sidebar"]',
            '[class*="conversation"]',
            '[class*="chat-list"]',
            '#lst-conversation',
            'div[data-id]',
            'div[tabindex]',
        ]
        while time.time() < deadline:
            for selector in shell_selectors:
                try:
                    if page.locator(selector).count() > 0:
                        time.sleep(1.5)
                        return
                except Exception:
                    continue
            time.sleep(1)

        time.sleep(3)

    def _navigate_to_contact_view(self, page: Page) -> tuple[bool, Optional[str]]:
        """Open the contacts or conversation surface that contains the list."""
        if self._contact_view_matches(page, CONTACT_VIEW_SELECTORS[0]):
            self._dismiss_contact_modal(page)
            return True, CONTACT_VIEW_SELECTORS[0]["name"]

        for strategy in CONTACT_VIEW_SELECTORS:
            for selector in strategy["nav"]:
                try:
                    locator = page.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.click(timeout=3_000)
                    time.sleep(2)
                    self._dismiss_contact_modal(page)
                    if self._contact_view_matches(page, strategy):
                        return True, strategy["name"]
                except Exception:
                    continue

        return False, None

    def _contact_view_matches(self, page: Page, strategy: dict) -> bool:
        """Confirm the UI looks like the contacts view, not just any sidebar list."""
        if self._has_visible_locator(page, '#main-tab .leftbar-tab.selected[title*="Tin nh"]'):
            return False

        marker_selectors = [
            '#main-tab .leftbar-tab.selected[title*="Danh b"]',
            '#contact-search',
            '#contact-search-input',
        ] + strategy.get("view_markers", [])

        if not any(self._has_visible_locator(page, selector) for selector in marker_selectors):
            return False

        if not self._find_contact_list_context(page, strategy):
            return False

        return True

    def _dismiss_contact_modal(self, page: Page):
        """Close the add-friend modal if contact navigation opened it by mistake."""
        if not self._has_visible_locator(page, '#FIND_FRIEND'):
            return

        close_selectors = [
            '#FIND_FRIEND .modal-header-icon',
            '[data-id="btn_Main_AddFrd_CXL"]',
        ]
        for selector in close_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=2_000)
                    time.sleep(1)
                    return
            except Exception:
                continue

    def _looks_like_conversation_view(self, page: Page) -> bool:
        hits = 0
        for selector in CONVERSATION_VIEW_MARKERS:
            if self._has_visible_locator(page, selector):
                hits += 1
        return hits >= 2

    def _find_contact_list_context(self, page: Page, strategy: dict) -> Optional[dict]:
        """Resolve the container and item selectors for the current UI layout."""
        tagged_context = self._tag_contact_list_context(page, strategy)
        if tagged_context:
            return tagged_context

        preferred_contexts = [
            ("#container .ReactVirtualized__Grid.ReactVirtualized__List", ".contact-item-v2-wrapper"),
            ("#container .ReactVirtualized__Grid.ReactVirtualized__List", ".contact-item-v2-alpha__section"),
            ("#container .virtualized-scroll", ".contact-item-v2-wrapper"),
        ]
        for container_selector, item_selector in preferred_contexts:
            try:
                container = page.locator(container_selector).first
                if container.count() == 0 or not container.is_visible():
                    continue
                if container.locator(item_selector).count() > 0:
                    return {
                        "strategy": strategy["name"],
                        "container_selector": container_selector,
                        "item_selector": item_selector,
                    }
            except Exception:
                continue

        # Do not fall back to generic Zalo grids here. Hidden conversation lists use
        # the same virtualized primitives and can produce false "contacts".
        return None

        for container_selector in strategy["container"]:
            try:
                container = page.locator(container_selector).first
                if container.count() == 0 or not container.is_visible():
                    continue
                for item_selector in strategy["item"]:
                    try:
                        if container.locator(item_selector).count() > 0:
                            return {
                                "strategy": strategy["name"],
                                "container_selector": container_selector,
                                "item_selector": item_selector,
                            }
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _tag_contact_list_context(self, page: Page, strategy: dict) -> Optional[dict]:
        """Find the actual contacts scroll container, skipping hidden chat and modal grids."""
        found = page.evaluate(
            """
            () => {
                const attr = 'data-mmbzalo-contact-container';
                document.querySelectorAll(`[${attr}]`).forEach((el) => el.removeAttribute(attr));

                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };

                const contacts = Array.from(document.querySelectorAll('#container .contact-item-v2-wrapper'))
                    .filter((el) => isVisible(el) && !el.closest('#FIND_FRIEND'));

                if (contacts.length === 0) return null;

                let node = contacts[0].parentElement;
                let scrollContainer = null;
                while (node && node.id !== 'container') {
                    const style = window.getComputedStyle(node);
                    const overflow = `${style.overflow} ${style.overflowY}`;
                    if (node.scrollHeight > node.clientHeight + 20 && /(auto|scroll)/.test(overflow)) {
                        scrollContainer = node;
                        break;
                    }
                    node = node.parentElement;
                }

                if (!scrollContainer) {
                    scrollContainer = contacts[0].closest('.virtualized-scroll') || contacts[0].parentElement;
                }

                scrollContainer.setAttribute(attr, '1');
                return {
                    rawCount: contacts.length,
                    scrollTop: scrollContainer.scrollTop || 0,
                    scrollHeight: scrollContainer.scrollHeight || 0,
                    clientHeight: scrollContainer.clientHeight || 0,
                };
            }
            """
        )

        if not found:
            return None

        return {
            "strategy": strategy["name"],
            "container_selector": '[data-mmbzalo-contact-container="1"]',
            "item_selector": ".contact-item-v2-wrapper",
        }

    def _has_visible_locator(self, page: Page, selector: str) -> bool:
        try:
            locator = page.locator(selector).first
            return locator.count() > 0 and locator.is_visible()
        except Exception:
            return False

    def _detect_empty_state(self, page: Page) -> bool:
        for selector in EMPTY_STATE_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _collect_contact_candidates(self, page: Page, list_context: dict) -> dict:
        contacts = page.evaluate(
            """
            ({ containerSelector, itemSelector }) => {
                const container = document.querySelector(containerSelector);
                if (!container) {
                    return { contacts: [], rawCount: 0, scrollTop: 0, scrollHeight: 0, clientHeight: 0 };
                }

                const nodes = Array.from(container.querySelectorAll(itemSelector));
                const contacts = nodes.map((node) => {
                    const collectTexts = (selector) => Array.from(node.querySelectorAll(selector))
                        .map((el) => (el.textContent || '').trim())
                        .filter(Boolean)
                        .filter((value) => value.length < 140);
                    const textCandidates = collectTexts('span, p, strong, h1, h2, h3, h4, h5, h6');
                    if (!textCandidates.length) {
                        textCandidates.push(...collectTexts('div'));
                    }
                    const initialsOnly = (value) => {
                        const compact = (value || '').replace(/\\s+/g, '');
                        return compact.length > 0
                            && compact.length <= 4
                            && /^[A-Z]+$/.test(compact);
                    };
                    const nameIndex = textCandidates.findIndex((value) => !initialsOnly(value));
                    const name = (nameIndex >= 0 ? textCandidates[nameIndex] : null)
                        || (node.textContent || '').trim().split('\\n')[0]
                        || '';
                    const lastMessage = textCandidates.find((value, index) => index !== nameIndex && !initialsOnly(value)) || null;
                    const imgEl = node.querySelector('img');
                    const unreadEl = node.querySelector('[class*="badge"], [class*="unread"], [aria-label*="unread"]');
                    return {
                        zid: node.getAttribute('data-id') || node.dataset?.id || null,
                        name,
                        avatar_url: imgEl?.src || null,
                        last_message: lastMessage,
                        unread: !!unreadEl,
                    };
                }).filter((item) => item.name && item.name.length < 140);

                const rawCount = nodes.length;
                const scrollTop = container.scrollTop || 0;
                const scrollHeight = container.scrollHeight || 0;
                const clientHeight = container.clientHeight || 0;
                return { contacts, rawCount, scrollTop, scrollHeight, clientHeight };
            }
            """,
            {
                "containerSelector": list_context["container_selector"],
                "itemSelector": list_context["item_selector"],
            },
        )
        return contacts

    def _scroll_contact_container(self, page: Page, list_context: dict, delta: int = 900) -> int:
        return page.evaluate(
            """
            ({ containerSelector, delta }) => {
                const container = document.querySelector(containerSelector);
                if (!container) return -1;
                const before = container.scrollTop || 0;
                container.scrollTop = before + delta;
                return container.scrollTop || 0;
            }
            """,
            {"containerSelector": list_context["container_selector"], "delta": delta},
        )

    def _set_contact_container_scroll(self, page: Page, list_context: dict, position: str) -> int:
        return page.evaluate(
            """
            ({ containerSelector, position }) => {
                const container = document.querySelector(containerSelector);
                if (!container) return -1;
                if (position === 'bottom') {
                    container.scrollTop = container.scrollHeight;
                } else if (position === 'top') {
                    container.scrollTop = 0;
                }
                return container.scrollTop || 0;
            }
            """,
            {"containerSelector": list_context["container_selector"], "position": position},
        )

    def _adaptive_scroll_delta(self, snapshot: dict) -> int:
        client_height = snapshot.get("clientHeight", 0) or 0
        if client_height <= 0:
            return 720
        return max(240, min(int(client_height * 0.85), 960))

    def _build_contact_key(self, raw: dict) -> str:
        if raw.get("zid"):
            return f"id:{raw['zid']}"
        name = normalize_contact_name(raw.get("name") or "").lower()
        avatar = (raw.get("avatar_url") or "").strip().lower()
        return f"name_avatar:{name}|{avatar}"

    def _contact_identity_source(self, raw: dict) -> str:
        return "zid" if raw.get("zid") else "name_avatar"

    def _merge_contact_record(self, existing: dict, incoming: dict) -> dict:
        existing["zid"] = incoming.get("zid") or existing.get("zid")
        existing["name"] = normalize_contact_name(incoming.get("name") or "") or existing.get("name")
        existing["avatar_url"] = incoming.get("avatar_url") or existing.get("avatar_url")
        existing["last_message"] = incoming.get("last_message") or existing.get("last_message")
        existing["unread"] = bool(existing.get("unread") or incoming.get("unread"))
        existing["identity_key"] = incoming.get("identity_key") or existing.get("identity_key")
        existing["identity_source"] = incoming.get("identity_source") or existing.get("identity_source")
        return existing

    def _capture_sync_debug(self, page: Page, label: str) -> list[str]:
        if not SYNC_DEBUG_ENABLED:
            return []

        os.makedirs(SYNC_DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "sync"
        screenshot_path = os.path.join(SYNC_DEBUG_DIR, f"{stamp}-{safe_label}.png")
        html_path = os.path.join(SYNC_DEBUG_DIR, f"{stamp}-{safe_label}.html")
        artifacts = []

        try:
            page.screenshot(path=screenshot_path, full_page=True)
            artifacts.append(screenshot_path)
        except Exception as exc:
            logger.warning(f"Failed to capture contact sync screenshot: {exc}")

        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            artifacts.append(html_path)
        except Exception as exc:
            logger.warning(f"Failed to capture contact sync HTML: {exc}")

        return artifacts

    def _capture_send_debug(self, page: Page, label: str) -> list[str]:
        if not SEND_DEBUG_ENABLED:
            return []

        os.makedirs(SYNC_DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "send"
        screenshot_path = os.path.join(SYNC_DEBUG_DIR, f"{stamp}-{safe_label}.png")
        html_path = os.path.join(SYNC_DEBUG_DIR, f"{stamp}-{safe_label}.html")
        artifacts = []

        try:
            page.screenshot(path=screenshot_path, full_page=True)
            artifacts.append(screenshot_path)
        except Exception as exc:
            logger.warning(f"Failed to capture send screenshot: {exc}")

        try:
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            artifacts.append(html_path)
        except Exception as exc:
            logger.warning(f"Failed to capture send HTML: {exc}")

        return artifacts

    # ═══════════════════════════════════════════════════════════
    #  CONTACTS
    # ═══════════════════════════════════════════════════════════

    def _sync_contacts_sync(self) -> dict:
        diagnostics = ContactSyncDiagnostics()
        context, page = self._worker_page()
        owns_context = context is not self._login_context
        try:
            diagnostics.login_detected = self._detect_auth(page)

            target_view_detected, strategy_name = self._navigate_to_contact_view(page)
            diagnostics.target_view_detected = target_view_detected
            diagnostics.selector_family = strategy_name

            if not target_view_detected:
                diagnostics.debug_artifacts = self._capture_sync_debug(page, "contact-view-not-found")
                message = "Contact sync failed: authenticated session loaded, but the contact list view could not be located."
                logger.warning(message)
                return {
                    "contacts": [],
                    "contact_count": 0,
                    "sync_status": "view_not_found",
                    "diagnostics": diagnostics,
                    "message": message,
                }

            list_context = None
            preferred_strategies = [s for s in CONTACT_VIEW_SELECTORS if s["name"] == strategy_name]
            fallback_strategies = [s for s in CONTACT_VIEW_SELECTORS if s["name"] != strategy_name]
            for strategy in preferred_strategies + fallback_strategies:
                list_context = self._find_contact_list_context(page, strategy)
                if list_context:
                    diagnostics.selector_family = strategy["name"]
                    break

            if not list_context:
                diagnostics.debug_artifacts = self._capture_sync_debug(page, "contact-container-not-found")
                message = "Contact sync failed: the contact view opened, but no readable contact list container was detected."
                logger.warning(message)
                return {
                    "contacts": [],
                    "contact_count": 0,
                    "sync_status": "container_not_found",
                    "diagnostics": diagnostics,
                    "message": message,
                }

            aggregate = {}
            raw_nodes_total = 0
            bottom_stable_passes = 0
            last_scroll_top = None
            max_passes = 160
            timeout_seconds = 115.0
            started_at = time.monotonic()
            phase = "forward"
            backfill_found_new_contacts = False
            sync_completed = False

            for pass_index in range(max_passes):
                elapsed_seconds = time.monotonic() - started_at
                if elapsed_seconds >= timeout_seconds:
                    diagnostics.ended_by_timeout = True
                    break

                snapshot = self._collect_contact_candidates(page, list_context)
                diagnostics.total_passes = pass_index + 1
                diagnostics.scroll_passes = diagnostics.total_passes
                raw_nodes_total += snapshot.get("rawCount", 0)

                before_count = len(aggregate)
                for raw_contact in snapshot.get("contacts", []):
                    raw_contact["name"] = choose_best_contact_name([raw_contact.get("name") or ""], fallback=raw_contact.get("name") or "")
                    key = self._build_contact_key(raw_contact)
                    raw_contact["identity_key"] = key
                    raw_contact["identity_source"] = self._contact_identity_source(raw_contact)
                    if key in aggregate:
                        aggregate[key] = self._merge_contact_record(aggregate[key], raw_contact)
                    else:
                        aggregate[key] = raw_contact

                after_count = len(aggregate)
                current_scroll_top = snapshot.get("scrollTop", 0)
                scroll_height = snapshot.get("scrollHeight", 0)
                client_height = snapshot.get("clientHeight", 0)
                at_bottom = scroll_height > 0 and (current_scroll_top + client_height >= scroll_height - 12)
                new_contacts_found = after_count - before_count
                diagnostics.bottom_reached = diagnostics.bottom_reached or at_bottom

                if phase == "forward":
                    diagnostics.forward_passes += 1
                    if at_bottom:
                        phase = "stabilize_bottom"
                        bottom_stable_passes = 1 if new_contacts_found == 0 else 0
                    else:
                        next_scroll_top = self._scroll_contact_container(
                            page,
                            list_context,
                            delta=self._adaptive_scroll_delta(snapshot),
                        )
                        last_scroll_top = next_scroll_top
                        time.sleep(0.6)
                        continue

                elif phase == "stabilize_bottom":
                    diagnostics.verification_passes += 1
                    if new_contacts_found == 0:
                        bottom_stable_passes += 1
                    else:
                        bottom_stable_passes = 0

                    if bottom_stable_passes >= 2:
                        phase = "backfill_up"
                    else:
                        self._set_contact_container_scroll(page, list_context, "bottom")
                        time.sleep(0.5)
                        continue

                elif phase == "backfill_up":
                    diagnostics.verification_passes += 1
                    upward_delta = -max(int(client_height * 1.5), 360)
                    next_scroll_top = self._scroll_contact_container(page, list_context, delta=upward_delta)
                    last_scroll_top = next_scroll_top
                    phase = "backfill_collect_up"
                    time.sleep(0.6)
                    continue

                elif phase == "backfill_collect_up":
                    diagnostics.verification_passes += 1
                    backfill_found_new_contacts = new_contacts_found > 0
                    self._set_contact_container_scroll(page, list_context, "bottom")
                    phase = "backfill_collect_down"
                    time.sleep(0.6)
                    continue

                elif phase == "backfill_collect_down":
                    diagnostics.verification_passes += 1
                    if new_contacts_found > 0 or backfill_found_new_contacts:
                        backfill_found_new_contacts = False
                        bottom_stable_passes = 0
                        phase = "stabilize_bottom"
                        self._set_contact_container_scroll(page, list_context, "bottom")
                        time.sleep(0.5)
                        continue

                    diagnostics.verification_stabilized = True
                    sync_completed = True
                    break

                if last_scroll_top == current_scroll_top and not at_bottom and phase == "forward":
                    self._set_contact_container_scroll(page, list_context, "bottom")
                    time.sleep(0.6)
                    continue

            else:
                diagnostics.ended_by_safety_limit = True

            diagnostics.raw_nodes_found = raw_nodes_total
            diagnostics.deduplicated_contacts = len(aggregate)
            diagnostics.empty_state_detected = self._detect_empty_state(page)
            diagnostics.elapsed_seconds = round(time.monotonic() - started_at, 2)
            diagnostics.unique_ids_found = len({item["zid"] for item in aggregate.values() if item.get("zid")})
            diagnostics.contacts_without_ids = sum(1 for item in aggregate.values() if not item.get("zid"))

            contacts = [
                ContactInfo(
                    name=normalize_contact_name(item["name"]) or item["name"],
                    zid=item.get("zid"),
                    avatar_url=item.get("avatar_url"),
                    last_message=item.get("last_message"),
                    unread=bool(item.get("unread")),
                    identity_key=item.get("identity_key"),
                    identity_source=item.get("identity_source"),
                )
                for item in aggregate.values()
            ]

            if sync_completed and contacts:
                message = (
                    f"Synced {len(contacts)} contact(s) completely. "
                    f"selector={diagnostics.selector_family}, passes={diagnostics.total_passes}, "
                    f"verify={diagnostics.verification_passes}, raw_nodes={diagnostics.raw_nodes_found}"
                )
                logger.info(message)
                return {
                    "contacts": contacts,
                    "contact_count": len(contacts),
                    "sync_status": "success",
                    "diagnostics": diagnostics,
                    "message": message,
                }

            if contacts:
                limit_reason = "time limit" if diagnostics.ended_by_timeout else "safety limit"
                if not diagnostics.ended_by_timeout and not diagnostics.ended_by_safety_limit:
                    limit_reason = "verification did not stabilize"
                message = (
                    f"Contact sync is partial: collected {len(contacts)} contact(s), "
                    f"but completeness could not be proven before {limit_reason}. "
                    f"selector={diagnostics.selector_family}, passes={diagnostics.total_passes}, "
                    f"bottom_reached={diagnostics.bottom_reached}, verify={diagnostics.verification_passes}"
                )
                logger.warning(message)
                return {
                    "contacts": contacts,
                    "contact_count": len(contacts),
                    "sync_status": "partial",
                    "diagnostics": diagnostics,
                    "message": message,
                }

            if diagnostics.empty_state_detected:
                message = "Contact sync completed: the contact list view was detected, but Zalo reported an empty contact list."
                logger.info(message)
                return {
                    "contacts": [],
                    "contact_count": 0,
                    "sync_status": "empty",
                    "diagnostics": diagnostics,
                    "message": message,
                }

            diagnostics.debug_artifacts = self._capture_sync_debug(page, "contact-sync-no-results")
            message = (
                "Contact sync failed: authenticated session and contact view were detected, "
                "but no contacts could be extracted from the rendered list."
            )
            logger.warning(message)
            return {
                "contacts": [],
                "contact_count": 0,
                "sync_status": "scrape_failed",
                "diagnostics": diagnostics,
                "message": message,
            }
        finally:
            if owns_context:
                context.close()

    async def sync_contacts(self) -> dict:
        return await _run_in_thread(self._sync_contacts_sync)

    # ═══════════════════════════════════════════════════════════
    #  MESSAGING
    # ═══════════════════════════════════════════════════════════

    def _open_target_via_search(self, page: Page, target: str) -> tuple[bool, Optional[str], Optional[str]]:
        search_target = normalize_contact_name(target) or target
        if not self._open_search(page):
            return False, "search_open_failed: Could not open search bar.", None

        page.keyboard.type(search_target, delay=50)
        time.sleep(2)
        selected = self._select_search_result(page, expected_text=search_target)
        if not selected.get("clicked"):
            artifacts = self._capture_send_debug(page, f"send-search-result-not-found-{target}")
            suffix = f" debug={', '.join(artifacts)}" if artifacts else ""
            return False, f"search_result_not_found: No visible result for '{target}'.{suffix}", None

        time.sleep(1.5)
        active_thread_target = selected.get("text") or search_target
        if not self._confirm_active_thread(page, search_target, target, active_thread_target):
            artifacts = self._capture_send_debug(page, f"send-thread-not-opened-{target}")
            suffix = f" debug={', '.join(artifacts)}" if artifacts else ""
            return False, f"target_thread_not_opened: Selected '{target}' but the chat thread did not open.{suffix}", None

        return True, None, search_target

    def _open_conversation_view(self, page: Page) -> bool:
        self._clear_search_input(page)
        if self._looks_like_conversation_view(page):
            return True

        selectors = [
            '#main-tab .leftbar-tab.selected[title*="Tin nh"]',
            '#main-tab .leftbar-tab[title*="Tin nh"]',
            '#main-tab .leftbar-tab[title*="Message"]',
            '[data-tab="messages"]',
            '[aria-label*="Tin nh"]',
            '[aria-label*="Message"]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0 or not locator.is_visible():
                    continue
                locator.click(timeout=3_000)
                time.sleep(1.5)
                self._clear_search_input(page)
                if self._looks_like_conversation_view(page):
                    return True
            except Exception:
                continue

        return self._looks_like_conversation_view(page)

    def _find_conversation_list_context(self, page: Page) -> Optional[dict]:
        return page.evaluate(
            """
            () => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const rowSelectors = [
                    '[data-id]',
                    '.conv-item',
                    '[class*="conv-item"]',
                    '[class*="conversation-item"]',
                    '[class*="ConversationItem"]',
                    '[class*="chat-item"]',
                    '[class*="ChatItem"]',
                    '[role="listitem"]',
                    '[role="option"]',
                    'div[tabindex="0"]',
                ];
                const containerSelectors = [
                    '#lst-conversation',
                    '#container [class*="conversation-list"]',
                    '#container [class*="ConversationList"]',
                    '#container [class*="chat-list"]',
                    '#container [class*="ChatList"]',
                    '#container aside',
                    '[class*="conversation-list"]',
                    '[class*="ConversationList"]',
                    '[class*="chat-list"]',
                    '[class*="ChatList"]',
                    '[role="list"]',
                    '[role="listbox"]',
                ];

                document.querySelectorAll('[data-mmbzalo-thread-container]').forEach((el) => {
                    el.removeAttribute('data-mmbzalo-thread-container');
                });

                const scoreContainer = (container) => {
                    const rect = container.getBoundingClientRect();
                    if (!isVisible(container) || rect.height < 180 || rect.width < 160) {
                        return null;
                    }
                    if (rect.left > window.innerWidth * 0.55) {
                        return null;
                    }

                    const rows = [];
                    const seen = new Set();
                    for (const selector of rowSelectors) {
                        for (const el of container.querySelectorAll(selector)) {
                            if (seen.has(el) || !isVisible(el)) continue;
                            const rowRect = el.getBoundingClientRect();
                            if (rowRect.width < 120 || rowRect.height < 28) continue;
                            if (!container.contains(el) || el === container) continue;
                            seen.add(el);
                            rows.push(el);
                        }
                    }

                    let score = rows.length * 100;
                    if (container.scrollHeight > container.clientHeight + 60) score += 25;
                    if (rect.left < window.innerWidth * 0.4) score += 10;
                    return {
                        container,
                        rowCount: rows.length,
                        score,
                    };
                };

                const candidates = [];
                const seenContainers = new Set();
                for (const selector of containerSelectors) {
                    for (const container of document.querySelectorAll(selector)) {
                        if (seenContainers.has(container)) continue;
                        seenContainers.add(container);
                        const scored = scoreContainer(container);
                        if (scored) candidates.push(scored);
                    }
                }

                if (!candidates.length) {
                    const heuristic = Array.from(document.querySelectorAll('div, aside, section'))
                        .filter((el) => !seenContainers.has(el))
                        .map((el) => scoreContainer(el))
                        .filter(Boolean);
                    candidates.push(...heuristic);
                }

                candidates.sort((a, b) => b.score - a.score);
                const best = candidates[0];
                if (!best || best.rowCount === 0) {
                    return null;
                }

                best.container.setAttribute('data-mmbzalo-thread-container', '1');
                return {
                    container_selector: '[data-mmbzalo-thread-container="1"]',
                    row_count: best.rowCount,
                    scroll_height: best.container.scrollHeight || 0,
                    client_height: best.container.clientHeight || 0,
                };
            }
            """
        )

    def _collect_visible_conversation_threads(self, page: Page, list_context: dict) -> dict:
        return page.evaluate(
            """
            ({ containerSelector }) => {
                const container = document.querySelector(containerSelector);
                if (!container) {
                    return { threads: [], scrollTop: 0, scrollHeight: 0, clientHeight: 0 };
                }

                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const collectTexts = (node, selector) => Array.from(node.querySelectorAll(selector))
                    .map((el) => (el.textContent || '').trim())
                    .filter(Boolean)
                    .filter((value) => value.length < 140);
                const initialsOnly = (value) => {
                    const compact = (value || '').replace(/\\s+/g, '');
                    return compact.length > 0 && compact.length <= 4 && /^[A-Z]+$/.test(compact);
                };
                const extractName = (node) => {
                    const textCandidates = collectTexts(node, 'span, p, strong, h1, h2, h3, h4, h5, h6');
                    if (!textCandidates.length) {
                        textCandidates.push(...collectTexts(node, 'div'));
                    }
                    const nameIndex = textCandidates.findIndex((value) => !initialsOnly(value));
                    return (nameIndex >= 0 ? textCandidates[nameIndex] : null)
                        || (node.textContent || '').trim().split('\\n')[0]
                        || '';
                };
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                const rowSelectors = [
                    '[data-id]',
                    '.conv-item',
                    '[class*="conv-item"]',
                    '[class*="conversation-item"]',
                    '[class*="ConversationItem"]',
                    '[class*="chat-item"]',
                    '[class*="ChatItem"]',
                    '[role="listitem"]',
                    '[role="option"]',
                    'div[tabindex="0"]',
                ];

                container.querySelectorAll('[data-mmbzalo-thread-key]').forEach((el) => {
                    el.removeAttribute('data-mmbzalo-thread-key');
                });

                const rows = [];
                const seen = new Set();
                for (const selector of rowSelectors) {
                    for (const el of container.querySelectorAll(selector)) {
                        if (seen.has(el) || !isVisible(el) || el.closest('#searchResultList, #FIND_FRIEND, [role="dialog"], [class*="modal"]')) {
                            continue;
                        }
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 120 || rect.height < 28) continue;
                        seen.add(el);
                        rows.push(el);
                    }
                }

                if (!rows.length) {
                    for (const el of Array.from(container.children)) {
                        if (!isVisible(el)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 120 || rect.height < 28) continue;
                        if (!(el.textContent || '').trim()) continue;
                        rows.push(el);
                    }
                }

                const threads = [];
                let index = 0;
                for (const row of rows) {
                    const name = extractName(row);
                    const normalized = normalize(name);
                    if (!normalized || normalized.length >= 140) continue;
                    const key = `thread-${index++}`;
                    row.setAttribute('data-mmbzalo-thread-key', key);
                    threads.push({
                        selector: `[data-mmbzalo-thread-key="${key}"]`,
                        data_id: row.getAttribute('data-id') || row.dataset?.id || null,
                        name,
                        normalized,
                        row_text: normalize((row.textContent || '').trim()),
                    });
                }

                return {
                    threads,
                    scrollTop: container.scrollTop || 0,
                    scrollHeight: container.scrollHeight || 0,
                    clientHeight: container.clientHeight || 0,
                };
            }
            """,
            {"containerSelector": list_context["container_selector"]},
        )

    def _set_conversation_list_scroll(self, page: Page, list_context: dict, value: int) -> int:
        return page.evaluate(
            """
            ({ containerSelector, value }) => {
                const container = document.querySelector(containerSelector);
                if (!container) return -1;
                container.scrollTop = Math.max(0, value);
                return container.scrollTop || 0;
            }
            """,
            {"containerSelector": list_context["container_selector"], "value": value},
        )

    def _scroll_conversation_list(self, page: Page, list_context: dict, delta: int) -> int:
        return page.evaluate(
            """
            ({ containerSelector, delta }) => {
                const container = document.querySelector(containerSelector);
                if (!container) return -1;
                const before = container.scrollTop || 0;
                container.scrollTop = Math.max(0, before + delta);
                return container.scrollTop || 0;
            }
            """,
            {"containerSelector": list_context["container_selector"], "delta": delta},
        )

    def _build_conversation_thread_index(self, page: Page, list_context: dict, max_passes: int = 10) -> dict[str, list[dict]]:
        self._set_conversation_list_scroll(page, list_context, 0)
        time.sleep(0.3)

        index: dict[str, list[dict]] = {}
        bottom_stable_passes = 0
        for pass_index in range(max_passes):
            snapshot = self._collect_visible_conversation_threads(page, list_context)
            current_scroll_top = snapshot.get("scrollTop", 0)
            scroll_height = snapshot.get("scrollHeight", 0)
            client_height = snapshot.get("clientHeight", 0)
            for item in snapshot.get("threads", []):
                normalized = item.get("normalized")
                if not normalized:
                    continue
                item["scroll_top"] = current_scroll_top
                bucket = index.setdefault(normalized, [])
                item_id = item.get("data_id")
                item_signature = item.get("row_text") or item.get("name")
                if not any(
                    (item_id and existing.get("data_id") == item_id)
                    or (
                        not item_id
                        and existing.get("normalized") == item.get("normalized")
                        and (existing.get("row_text") or existing.get("name")) == item_signature
                    )
                    for existing in bucket
                ):
                    bucket.append(item)

            logger.info(
                "Campaign thread index crawl pass %s/%s: visible=%s indexed_names=%s scroll_top=%s scroll_height=%s",
                pass_index + 1,
                max_passes,
                len(snapshot.get("threads", [])),
                len(index),
                current_scroll_top,
                scroll_height,
            )

            at_bottom = scroll_height > 0 and (current_scroll_top + client_height >= scroll_height - 12)
            if at_bottom:
                bottom_stable_passes += 1
                if bottom_stable_passes >= 2:
                    break
            else:
                bottom_stable_passes = 0

            delta = max(240, min(int((client_height or 360) * 0.85), 960))
            next_scroll_top = self._scroll_conversation_list(page, list_context, delta)
            if next_scroll_top == current_scroll_top:
                bottom_stable_passes += 1
                if bottom_stable_passes >= 2:
                    break
            time.sleep(0.35)

        self._set_conversation_list_scroll(page, list_context, 0)
        time.sleep(0.2)
        return index

    def _open_indexed_conversation_thread(
        self,
        page: Page,
        list_context: dict,
        entry: dict,
        target: str,
        max_passes: int = 8,
    ) -> tuple[bool, Optional[str]]:
        target_scroll = int(entry.get("scroll_top", 0) or 0)
        self._set_conversation_list_scroll(page, list_context, max(target_scroll - 80, 0))
        time.sleep(0.25)

        expected_id = entry.get("data_id")
        expected_name = entry.get("normalized")
        client_height = 0

        for pass_index in range(max_passes):
            snapshot = self._collect_visible_conversation_threads(page, list_context)
            client_height = snapshot.get("clientHeight", 0) or client_height
            candidates = []
            if expected_id:
                candidates = [item for item in snapshot.get("threads", []) if item.get("data_id") == expected_id]
            if not candidates and expected_name:
                candidates = [item for item in snapshot.get("threads", []) if item.get("normalized") == expected_name]

            if len(candidates) == 1:
                candidate = candidates[0]
                try:
                    locator = page.locator(candidate["selector"]).first
                    if locator.count() == 0 or not locator.is_visible():
                        return False, "direct_thread_not_visible"
                    locator.click(timeout=3_000)
                    time.sleep(1.0)
                except Exception as exc:
                    logger.info(f"Campaign direct thread click failed for {target}: {exc}")
                    return False, "direct_thread_click_failed"

                active_thread_target = candidate.get("name") or target
                if not self._confirm_active_thread(page, target, active_thread_target):
                    return False, "direct_thread_not_opened"
                return True, None

            current_scroll_top = snapshot.get("scrollTop", 0)
            scroll_height = snapshot.get("scrollHeight", 0)
            if scroll_height > 0 and current_scroll_top + (client_height or 0) >= scroll_height - 12:
                break

            delta = max(240, min(int((client_height or 360) * 0.85), 960))
            next_scroll_top = self._scroll_conversation_list(page, list_context, delta)
            if next_scroll_top == current_scroll_top:
                break
            time.sleep(0.3)

        return False, "direct_thread_unavailable"

    def _send_messages_sync(self, targets, message, delay_min, delay_max) -> dict:
        context, page = self._worker_page()
        results = []
        try:
            for i, target in enumerate(targets):
                logger.info(f"Messaging {i+1}/{len(targets)}: {target}")
                success, error = False, None
                try:
                    opened, open_error, send_target = self._open_target_via_search(page, target)
                    if not opened:
                        raise RuntimeError(open_error or f"search_result_not_found: No visible result for '{target}'.")
                    sent, send_error = self._type_and_send_detailed(page, message, target=send_target)
                    if not sent:
                        artifacts = self._capture_send_debug(page, f"send-composer-not-found-{target}")
                        suffix = f" debug={', '.join(artifacts)}" if artifacts else ""
                        raise RuntimeError(f"{send_error}{suffix}")
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

    def _send_campaign_messages_sync(self, contacts, message, delay_min, delay_max) -> dict:
        context, page = self._worker_page()
        results = []
        route_stats = {"direct_thread": 0, "search_fallback": 0, "not_found": 0}
        started_at = time.monotonic()
        open_timings_ms: list[int] = []
        try:
            campaign_contacts = []
            for item in contacts:
                if isinstance(item, CampaignContactPreview):
                    campaign_contacts.append(item)
                else:
                    campaign_contacts.append(CampaignContactPreview(**item))

            list_context = None
            thread_index: dict[str, list[dict]] = {}
            if self._open_conversation_view(page):
                list_context = self._find_conversation_list_context(page)
                if list_context:
                    crawl_started_at = time.monotonic()
                    thread_index = self._build_conversation_thread_index(page, list_context)
                    crawl_elapsed = round((time.monotonic() - crawl_started_at) * 1000)
                    logger.info(
                        "Campaign thread index ready: %s visible names, %s unique, %s ambiguous, crawl_ms=%s.",
                        len(thread_index),
                        sum(1 for entries in thread_index.values() if len(entries) == 1),
                        sum(1 for entries in thread_index.values() if len(entries) > 1),
                        crawl_elapsed,
                    )
                else:
                    logger.info("Campaign conversation list context not found; falling back to search-only mode.")
            else:
                logger.info("Campaign conversation view unavailable; falling back to search-only mode.")

            for i, contact in enumerate(campaign_contacts):
                target = normalize_contact_name(contact.name) or contact.name
                logger.info(f"Campaign messaging {i+1}/{len(campaign_contacts)}: {target}")
                success, error = False, None
                route = "not_found"
                open_started_at = time.monotonic()
                try:
                    if thread_index and list_context:
                        self._open_conversation_view(page)
                    direct_entries = thread_index.get(self._normalize_visible_text(target), [])
                    used_direct_thread = False
                    if len(direct_entries) == 1:
                        opened, direct_error = self._open_indexed_conversation_thread(page, list_context, direct_entries[0], target)
                        if opened:
                            used_direct_thread = True
                            route = "direct_thread"
                            route_open_ms = round((time.monotonic() - open_started_at) * 1000)
                            open_timings_ms.append(route_open_ms)
                            logger.info(
                                "Campaign route direct_thread: %s open_ms=%s",
                                target,
                                route_open_ms,
                            )
                        else:
                            logger.info(f"Campaign direct thread unavailable for {target}: {direct_error}")
                    elif len(direct_entries) > 1:
                        logger.info(f"Campaign direct thread ambiguous for {target}; using search fallback.")

                    if not used_direct_thread:
                        opened, open_error, send_target = self._open_target_via_search(page, target)
                        if not opened:
                            raise RuntimeError(open_error or f"search_result_not_found: No visible result for '{target}'.")
                        route = "search_fallback"
                        route_open_ms = round((time.monotonic() - open_started_at) * 1000)
                        open_timings_ms.append(route_open_ms)
                        logger.info(
                            "Campaign route search_fallback: %s open_ms=%s",
                            target,
                            route_open_ms,
                        )
                    else:
                        send_target = target

                    sent, send_error = self._type_and_send_detailed(page, message, target=send_target)
                    if not sent:
                        artifacts = self._capture_send_debug(page, f"campaign-send-composer-not-found-{target}")
                        suffix = f" debug={', '.join(artifacts)}" if artifacts else ""
                        raise RuntimeError(f"{send_error}{suffix}")
                    success = True
                    logger.info(f"Campaign message sent to {target}")
                except Exception as exc:
                    error = str(exc)
                    if "search_result_not_found" in error:
                        route = "not_found"
                        logger.info(f"Campaign route not_found: {target}")
                    logger.warning(f"Campaign failed: {target}: {exc}")

                route_stats[route] = route_stats.get(route, 0) + 1
                results.append(
                    {
                        "identity_key": contact.identity_key,
                        "target": contact.name,
                        "success": success,
                        "error": error,
                        "route": route,
                    }
                )
                if i < len(campaign_contacts) - 1:
                    time.sleep(random.uniform(delay_min, delay_max))

            sent = sum(1 for item in results if item["success"])
            failed = len(results) - sent
            total_elapsed_ms = round((time.monotonic() - started_at) * 1000)
            average_open_ms = round(sum(open_timings_ms) / max(len(open_timings_ms), 1)) if open_timings_ms else 0
            message_summary = (
                f"Sent {sent}/{len(campaign_contacts)} ({failed} failed). "
                f"routes={route_stats} total_ms={total_elapsed_ms} avg_open_ms={average_open_ms}"
            )
            return {
                "total": len(campaign_contacts),
                "sent": sent,
                "failed": failed,
                "results": results,
                "route_stats": route_stats,
                "timing_ms": {"total": total_elapsed_ms, "avg_open": average_open_ms},
                "message": message_summary,
            }
        finally:
            context.close()

    async def send_campaign_messages(self, contacts, message, delay_min=15.0, delay_max=30.0) -> dict:
        return await _run_in_thread(self._send_campaign_messages_sync, contacts, message, delay_min, delay_max)

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

    def _clear_search_input(self, page: Page) -> bool:
        cleared = False
        for sel in ['input[placeholder*="T\\u00ECm ki\\u1EBFm"]', 'input[placeholder*="Search"]',
                    'input[type="search"]', '[class*="search"] input', '[class*="Search"] input',
                    '#contact-search-input']:
            try:
                el = page.locator(sel).first
                if el.count() == 0 or not el.is_visible():
                    continue
                el.click()
                el.fill("")
                cleared = True
                time.sleep(0.2)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                break
            except Exception:
                continue
        return cleared

    def _normalize_visible_text(self, value: Optional[str]) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).casefold()

    def _select_search_result(self, page: Page, expected_text: Optional[str] = None) -> dict:
        expected = self._normalize_visible_text(expected_text)
        return page.evaluate(
            """
            ({ expected }) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLocaleLowerCase();
                const selectors = [
                    '#searchResultList .conv-item',
                    '#searchResultList [id^="friend-item-"]',
                    '[role="listbox"] [role="option"]',
                    '[class*="search-result"] > div',
                    '[class*="SearchResult"] > div',
                    '[class*="search-item"]',
                    '[class*="SearchItem"]',
                    '#contact-search + div [data-id]',
                    '#contact-search ~ div [data-id]',
                ];

                const unique = [];
                const seen = new Set();
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (seen.has(el) || !isVisible(el) || el.closest('#FIND_FRIEND')) continue;
                        seen.add(el);
                        unique.push({ el, selector });
                    }
                }

                const scored = unique.map(({ el, selector }) => {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const normalized = normalize(text);
                    const inSearchSurface = !!el.closest('#searchResultList, [role="listbox"], [class*="search-result"], [class*="SearchResult"], [class*="search-item"], [class*="SearchItem"]');
                    let score = inSearchSurface ? 5 : 0;
                    if (el.matches?.('#searchResultList .conv-item, #searchResultList [id^="friend-item-"]')) {
                        score += 40;
                    }
                    if (expected) {
                        if (normalized === expected) score += 100;
                        else if (normalized.startsWith(expected)) score += 50;
                        else if (normalized.includes(expected)) score += 20;
                    } else {
                        score += 10;
                    }
                    return { el, selector, text, normalized, score };
                }).sort((a, b) => b.score - a.score);

                const best = scored[0];
                if (!best || (expected && best.score < 20)) {
                    return { clicked: false, selector: null, text: null };
                }

                const rect = best.el.getBoundingClientRect();
                const clickTarget = document.elementFromPoint(
                    rect.left + Math.min(rect.width * 0.5, Math.max(rect.width - 16, 16)),
                    rect.top + Math.min(rect.height * 0.5, Math.max(rect.height - 8, 8))
                ) || best.el;

                clickTarget.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                clickTarget.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                clickTarget.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                clickTarget.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }));
                best.el.click();
                return { clicked: true, selector: best.selector, text: best.text, id: best.el.id || null };
            }
            """,
            {"expected": expected},
        )

    def _click_search_result(self, page: Page) -> bool:
        return bool(self._select_search_result(page).get("clicked"))

    def _confirm_active_thread(self, page: Page, *targets: str, timeout_ms: int = 8_000) -> bool:
        target_texts = [self._normalize_visible_text(target) for target in targets if self._normalize_visible_text(target)]
        deadline = time.time() + max(timeout_ms / 1000, 3)

        while time.time() < deadline:
            if self._has_visible_locator(page, '#FIND_FRIEND, [role="dialog"], [class*="modal"]'):
                return False

            thread = page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const headerSelectors = [
                        '#container header',
                        '#container [class*="chat-header"]',
                        '#container [class*="ChatHeader"]',
                        '#container [class*="conversation-header"]',
                        '#container [class*="ConversationHeader"]',
                        '#container [class*="header"] [class*="title"]',
                    ];
                    const headerTexts = [];
                    for (const selector of headerSelectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!isVisible(el)) continue;
                            const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (text) headerTexts.push(text);
                        }
                    }

                    const composerSelectors = [
                        '#chatTextInput',
                        '[data-testid="message-input"]',
                        '[class*="chat-input"] div[contenteditable="true"]',
                        '[class*="ChatInput"] div[contenteditable="true"]',
                        'footer div[contenteditable="true"]',
                        'div[role="textbox"]',
                    ];
                    let composerVisible = false;
                    for (const selector of composerSelectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (isVisible(el)) {
                                composerVisible = true;
                                break;
                            }
                        }
                        if (composerVisible) break;
                    }

                    return { headerTexts, composerVisible };
                }
                """
            )

            header_texts = [self._normalize_visible_text(item) for item in thread.get("headerTexts", [])]
            if any(target_text in header for target_text in target_texts for header in header_texts):
                return True

            time.sleep(0.5)

        return False

    def _locate_message_composer(self, page: Page):
        return page.evaluate(
            """
            () => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const paneSelectors = [
                    '#container main',
                    '#container [class*="chat-box"]',
                    '#container [class*="ChatBox"]',
                    '#container [class*="conversation"]',
                    '#container [class*="Conversation"]',
                    '#container section',
                ];
                const composerSelectors = [
                    '#chatTextInput',
                    '[data-testid="message-input"]',
                    '[class*="chat-input"] div[contenteditable="true"]',
                    '[class*="ChatInput"] div[contenteditable="true"]',
                    'footer div[contenteditable="true"]',
                    'div[role="textbox"]',
                    'div[contenteditable="true"]',
                ];

                const panes = [];
                for (const selector of paneSelectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (!isVisible(el)) continue;
                        panes.push(el);
                    }
                }

                const byArea = panes.sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (br.width * br.height) - (ar.width * ar.height);
                });

                for (const pane of byArea) {
                    for (const selector of composerSelectors) {
                        const match = pane.matches?.(selector) ? pane : pane.querySelector(selector);
                        if (match && isVisible(match)) {
                            match.setAttribute('data-mmbzalo-message-composer', '1');
                            return {
                                selector,
                                composerSelector: '[data-mmbzalo-message-composer="1"]',
                            };
                        }
                    }
                }

                return null;
            }
            """
        )

    def _type_and_send_detailed(self, page: Page, message: str, target: Optional[str] = None) -> tuple[bool, Optional[str]]:
        composer = self._locate_message_composer(page)
        if not composer:
            return False, "message_composer_not_found: Could not locate the active message composer."

        try:
            el = page.locator(composer["composerSelector"]).first
            if el.count() == 0 or not el.is_visible():
                return False, "message_composer_not_found: Composer candidate was not visible."

            el.click()
            try:
                el.fill("")
            except Exception:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            page.keyboard.type(message, delay=30)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(1)
            return True, None
        except Exception as exc:
            logger.warning(f"Message submit failed for {target or 'target'}: {exc}")
            return False, "message_submit_failed: The composer was found, but submitting the message failed."

    def _type_and_send(self, page: Page, message: str) -> bool:
        sent, _ = self._type_and_send_detailed(page, message)
        return sent


# ═══════════════════════════════════════════════════════════════
#  SINGLETON
# ═══════════════════════════════════════════════════════════════

_driver: Optional[ZaloDriver] = None


async def get_driver() -> ZaloDriver:
    global _driver
    if _driver is None:
        _driver = ZaloDriver()
    return _driver
