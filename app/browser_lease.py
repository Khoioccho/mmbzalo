from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path

from app.browser_errors import ProfileLockedError


class BrowserProfileInUseError(ProfileLockedError):
    pass


class BrowserProfileLease:
    """Cross-process ownership lease for one workspace browser profile."""

    def __init__(self, lock_dir: Path, owner_type: str) -> None:
        self.lock_dir = lock_dir
        self.metadata_path = lock_dir / "owner.json"
        self.owner_type = owner_type
        self.token = uuid.uuid4().hex
        self.acquired = False

    @classmethod
    def acquire(
        cls,
        profiles_root: Path,
        workspace_key: str,
        owner_type: str,
        *,
        timeout_seconds: float = 0,
        retry_seconds: float = 2,
    ) -> BrowserProfileLease:
        lock_dir = profiles_root.resolve() / ".browser-locks" / f"{workspace_key}.lock"
        lock_dir.parent.mkdir(parents=True, exist_ok=True)
        lease = cls(lock_dir, owner_type)
        deadline = time.monotonic() + max(timeout_seconds, 0)

        while True:
            try:
                lock_dir.mkdir()
            except FileExistsError:
                if lease._reclaim_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise BrowserProfileInUseError(
                        "Workspace browser profile is currently in use. "
                        "Finish or stop Zalo login, then retry the operation."
                    )
                time.sleep(min(retry_seconds, max(deadline - time.monotonic(), 0)))
                continue

            lease.acquired = True
            try:
                lease.metadata_path.write_text(
                    json.dumps(
                        {
                            "token": lease.token,
                            "owner_type": owner_type,
                            "pid": os.getpid(),
                            "hostname": socket.gethostname(),
                            "created_at": time.time(),
                        }
                    ),
                    encoding="utf-8",
                )
            except Exception:
                lease.release()
                raise
            return lease

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            metadata = self._read_metadata()
            if metadata and metadata.get("token") != self.token:
                return
            self.metadata_path.unlink(missing_ok=True)
            self.lock_dir.rmdir()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def _read_metadata(self) -> dict | None:
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _reclaim_stale_lock(self) -> bool:
        metadata = self._read_metadata()
        if metadata:
            hostname = metadata.get("hostname")
            pid = metadata.get("pid")
            if hostname != socket.gethostname() or not isinstance(pid, int) or self._pid_is_alive(pid):
                return False
        else:
            try:
                if time.time() - self.lock_dir.stat().st_mtime < 60:
                    return False
            except FileNotFoundError:
                return True

        try:
            self.metadata_path.unlink(missing_ok=True)
            self.lock_dir.rmdir()
            return True
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return ctypes.windll.kernel32.GetLastError() == 5
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
