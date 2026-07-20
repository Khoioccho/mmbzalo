import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.browser_lease import BrowserProfileInUseError, BrowserProfileLease
from app.models import LoginState
from app.zalo_driver import ZaloDriver, _PlaywrightThread


class FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeQr:
    def is_visible(self) -> bool:
        return True

    def bounding_box(self) -> dict:
        return {"width": 240, "height": 240}

    def screenshot(self, **kwargs) -> bytes:
        return b"png-data"


class FakeMatches:
    def count(self) -> int:
        return 1

    def nth(self, index: int) -> FakeQr:
        return FakeQr()


class EmptyMatches:
    def count(self) -> int:
        return 0


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def locator(self, selector: str) -> FakeMatches:
        return FakeMatches()


class PanelFallbackPage(FakePage):
    def locator(self, selector: str):
        if selector == "main":
            return FakeMatches()
        return EmptyMatches()


class FullPageFallbackPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.full_page_requested = False

    def locator(self, selector: str) -> EmptyMatches:
        return EmptyMatches()

    def screenshot(self, **kwargs) -> bytes:
        self.full_page_requested = kwargs.get("full_page") is True
        return b"full-page-png"


class LockedChromium:
    def launch_persistent_context(self, **kwargs):
        raise RuntimeError("SingletonLock: profile is already in use")


class LockedPlaywright:
    def __init__(self) -> None:
        self.chromium = LockedChromium()


class FakeSharedPlaywright:
    def __init__(self) -> None:
        self.stop_count = 0

    def stop(self) -> None:
        self.stop_count += 1


class FakePlaywrightStarter:
    def __init__(self, playwright: FakeSharedPlaywright, starts: list[int]) -> None:
        self.playwright = playwright
        self.starts = starts

    def start(self) -> FakeSharedPlaywright:
        self.starts.append(1)
        return self.playwright


class FailingPlaywrightStarter:
    def start(self):
        raise RuntimeError("driver transport failed")


class LoginOnboardingTests(unittest.TestCase):
    def test_browser_profile_lease_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = BrowserProfileLease.acquire(root, "workspace-a", "login_browser")

            with self.assertRaises(BrowserProfileInUseError):
                BrowserProfileLease.acquire(root, "workspace-a", "contact_sync_worker")

            first.release()
            second = BrowserProfileLease.acquire(root, "workspace-a", "contact_sync_worker")
            metadata = json.loads(second.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["owner_type"], "contact_sync_worker")
            second.release()

    def test_qr_capture_returns_png_data_url(self) -> None:
        driver = object.__new__(ZaloDriver)
        driver._login_page = FakePage()

        expected = "data:image/png;base64," + base64.b64encode(b"png-data").decode("ascii")
        self.assertEqual(driver._capture_login_qr_sync(), expected)

    def test_qr_capture_falls_back_to_login_panel(self) -> None:
        driver = object.__new__(ZaloDriver)
        driver._login_page = PanelFallbackPage()

        expected = "data:image/png;base64," + base64.b64encode(b"png-data").decode("ascii")
        self.assertEqual(driver._capture_login_qr_sync(), expected)
        self.assertEqual(driver._last_qr_capture_mode, "login_panel")

    def test_qr_capture_falls_back_to_full_page(self) -> None:
        driver = object.__new__(ZaloDriver)
        page = FullPageFallbackPage()
        driver._login_page = page

        expected = "data:image/png;base64," + base64.b64encode(b"full-page-png").decode("ascii")
        self.assertEqual(driver._capture_login_qr_sync(), expected)
        self.assertTrue(page.full_page_requested)
        self.assertEqual(driver._last_qr_capture_mode, "full_page")

    def test_status_can_skip_qr_capture_for_background_checks(self) -> None:
        driver = object.__new__(ZaloDriver)
        driver._login_state = LoginState.WAITING_QR
        driver._profile_name = None
        driver._profile_avatar = None
        driver._capture_login_qr_sync = lambda: self.fail("QR capture should be skipped")

        result = driver._status_dict("Waiting", include_qr=False)
        self.assertIsNone(result["qr_image_base64"])

    def test_chrome_singleton_lock_becomes_clear_retryable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_path = root / "workspace-a"
            profile_path.mkdir()
            driver = object.__new__(ZaloDriver)
            driver.workspace_key = "workspace-a"
            driver.profile_path = profile_path
            driver._pw = LockedPlaywright()
            driver._deployment_settings = SimpleNamespace(
                browser_profiles_root=root,
                host_identity="test-host",
                login_display=":99",
            )
            driver._ensure_pw = lambda: None
            driver._get_launch_proxy_config = lambda: None

            with patch("app.zalo_driver.PROFILE_LOCK_TIMEOUT_SECONDS", 0):
                with self.assertRaisesRegex(BrowserProfileInUseError, "currently in use"):
                    driver._get_worker_context("contact_sync_worker")

            lease = BrowserProfileLease.acquire(root, "workspace-a", "test-owner")
            lease.release()

    def test_two_workspace_drivers_share_one_owner_runtime(self) -> None:
        runtime = _PlaywrightThread()
        shared_playwright = FakeSharedPlaywright()
        starts: list[int] = []
        driver_a = object.__new__(ZaloDriver)
        driver_a.workspace_key = "workspace-a"
        driver_a._pw = None
        driver_b = object.__new__(ZaloDriver)
        driver_b.workspace_key = "workspace-b"
        driver_b._pw = None

        async def scenario() -> None:
            await asyncio.wrap_future(runtime.submit(driver_a._ensure_pw))
            await asyncio.wrap_future(runtime.submit(driver_b._ensure_pw))
            await asyncio.wrap_future(runtime.submit(runtime.stop_playwright_sync))

        with patch("app.zalo_driver._playwright_thread", runtime), patch(
            "app.zalo_driver.sync_playwright",
            side_effect=lambda: FakePlaywrightStarter(shared_playwright, starts),
        ):
            asyncio.run(scenario())

        self.assertIs(driver_a._pw, shared_playwright)
        self.assertIs(driver_b._pw, shared_playwright)
        self.assertEqual(len(starts), 1)
        self.assertEqual(shared_playwright.stop_count, 1)

    def test_owner_runtime_recovers_after_initialization_failure(self) -> None:
        runtime = _PlaywrightThread()
        shared_playwright = FakeSharedPlaywright()
        attempts = [FailingPlaywrightStarter(), FakePlaywrightStarter(shared_playwright, [])]

        async def scenario() -> None:
            with self.assertRaisesRegex(RuntimeError, "Browser service"):
                await asyncio.wrap_future(runtime.submit(runtime.get_playwright_sync))
            recovered = await asyncio.wrap_future(runtime.submit(runtime.get_playwright_sync))
            self.assertIs(recovered, shared_playwright)
            await asyncio.wrap_future(runtime.submit(runtime.stop_playwright_sync))

        with patch("app.zalo_driver.sync_playwright", side_effect=lambda: attempts.pop(0)):
            asyncio.run(scenario())

        self.assertEqual(shared_playwright.stop_count, 1)

    def test_authenticated_login_closes_context_and_releases_lease(self) -> None:
        driver = object.__new__(ZaloDriver)
        page = FakePage()
        context = FakeContext()
        lease = FakeLease()
        driver._login_state = LoginState.WAITING_QR
        driver._profile_name = None
        driver._profile_avatar = None
        driver._login_page = page
        driver._login_context = context
        driver._login_lease = lease
        driver._detect_auth = lambda current_page: True

        def extract_profile(current_page) -> None:
            driver._profile_name = "Test User"

        driver._extract_profile = extract_profile
        result = driver._check_login_sync()

        self.assertEqual(result["state"], "authenticated")
        self.assertIsNone(result["qr_image_base64"])
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(lease.released)
        self.assertIsNone(driver._login_page)
        self.assertIsNone(driver._login_context)
        self.assertIsNone(driver._login_lease)


if __name__ == "__main__":
    unittest.main()
