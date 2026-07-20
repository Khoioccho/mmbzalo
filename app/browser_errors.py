from __future__ import annotations


class ServiceError(RuntimeError):
    error_code = "SERVICE_ERROR"
    user_message = "The operation could not be completed."
    retryable = False
    http_status = 400

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.user_message)

    def response_payload(self) -> dict:
        return {
            "error_code": self.error_code,
            "message": str(self),
            "retryable": self.retryable,
        }


class BrowserServiceError(ServiceError):
    error_code = "BROWSER_SERVICE_ERROR"
    user_message = "Browser service is temporarily unavailable."
    retryable = True
    http_status = 503

class PlaywrightRuntimeUnavailableError(BrowserServiceError):
    error_code = "PLAYWRIGHT_RUNTIME_UNAVAILABLE"


class PlaywrightThreadMismatchError(BrowserServiceError):
    error_code = "PLAYWRIGHT_THREAD_MISMATCH"


class WorkspaceBrowserBusyError(BrowserServiceError):
    error_code = "WORKSPACE_BROWSER_BUSY"
    user_message = "This workspace already has an active browser operation."
    http_status = 409


class ProfileLockedError(WorkspaceBrowserBusyError):
    error_code = "PROFILE_LOCKED"
    user_message = "The workspace browser profile is currently in use."


class BrowserStartFailedError(BrowserServiceError):
    error_code = "BROWSER_START_FAILED"


class ZaloNotAuthenticatedError(BrowserServiceError):
    error_code = "ZALO_NOT_AUTHENTICATED"
    user_message = "Connect Zalo before running this operation."
    retryable = False
    http_status = 409


class JobAlreadyRunningError(ServiceError):
    error_code = "JOB_ALREADY_RUNNING"
    user_message = "This workspace already has an active automation job."
    retryable = True
    http_status = 409

    def __init__(self, existing_job_id: str) -> None:
        self.existing_job_id = existing_job_id
        super().__init__(self.user_message)

    def response_payload(self) -> dict:
        return {**super().response_payload(), "existing_job_id": self.existing_job_id}
