from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from app.browser_lease import BrowserProfileInUseError
from app.config import get_settings
from app.database import session_scope
from app.db_models import AutomationJob, CampaignStatus, Contact, JobFailureClass, JobStatus, JobType, WorkspaceLoginState
from app.models import CampaignContactPreview
from app.services import (
    claim_next_job,
    ensure_workspace_session,
    get_campaign,
    get_workspace_runtime_settings,
    heartbeat_worker,
    mark_job_cancelled,
    mark_job_failed,
    mark_job_running,
    mark_job_succeeded,
    persist_campaign_execution,
    persist_synced_contacts,
    prepare_campaign_job,
    record_campaign_job_event,
    renew_job_lease,
)
from app.zalo_driver import get_driver


logger = logging.getLogger("worker")
settings = get_settings()


def _normalize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _job_failure_class(exc: Exception) -> JobFailureClass:
    if isinstance(exc, BrowserProfileInUseError):
        return JobFailureClass.TRANSIENT
    if "session expired" in str(exc).lower():
        return JobFailureClass.SESSION_EXPIRED
    return JobFailureClass.PERMANENT


def _settings_provider(workspace_id: UUID):
    def provider():
        with session_scope() as db:
            return get_workspace_runtime_settings(db, workspace_id)

    return provider


async def _driver_for_workspace(workspace_id: UUID):
    with session_scope() as db:
        ensure_workspace_session(db, workspace_id, settings)
    return await get_driver(str(workspace_id), settings, _settings_provider(workspace_id))


def _mark_workspace_session_expired(workspace_id: UUID, message: str) -> None:
    with session_scope() as db:
        session_row = ensure_workspace_session(db, workspace_id)
        session_row.login_state = WorkspaceLoginState.EXPIRED
        session_row.error_message = message
        db.flush()


async def _process_contact_sync(job_id: UUID) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_running(db, job)
        workspace_id = job.workspace_id
        user_id = job.created_by_user_id
    driver = await _driver_for_workspace(workspace_id)
    try:
        result = _normalize(await driver.sync_contacts())
    except Exception as exc:
        message = str(exc)
        failure_class = _job_failure_class(exc)
        with session_scope() as db:
            job = db.get(AutomationJob, job_id)
            if failure_class == JobFailureClass.SESSION_EXPIRED:
                _mark_workspace_session_expired(workspace_id, message)
            mark_job_failed(db, job, message=message, failure_class=failure_class)
        return
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        contacts_result = persist_synced_contacts(db, workspace_id, user_id, job, result)
        mark_job_succeeded(db, job, _normalize(contacts_result.model_dump()))


async def _process_manual_send(job_id: UUID) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_running(db, job)
        payload = dict(job.payload_json or {})
        workspace_id = job.workspace_id
    driver = await _driver_for_workspace(workspace_id)
    try:
        result = _normalize(
            await driver.send_messages(
                payload.get("targets", []),
                payload.get("message", ""),
                payload.get("delay_min", 15.0),
                payload.get("delay_max", 30.0),
            )
        )
    except Exception as exc:
        message = str(exc)
        failure_class = _job_failure_class(exc)
        with session_scope() as db:
            job = db.get(AutomationJob, job_id)
            if failure_class == JobFailureClass.SESSION_EXPIRED:
                _mark_workspace_session_expired(workspace_id, message)
            mark_job_failed(db, job, message=message, failure_class=failure_class)
        return
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_succeeded(db, job, result)


async def _process_friend_request(job_id: UUID) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_running(db, job)
        payload = dict(job.payload_json or {})
        workspace_id = job.workspace_id
    driver = await _driver_for_workspace(workspace_id)
    try:
        result = _normalize(
            await driver.send_friend_requests(
                payload.get("phone_numbers", []),
                payload.get("greeting_message"),
            )
        )
    except Exception as exc:
        message = str(exc)
        failure_class = _job_failure_class(exc)
        with session_scope() as db:
            job = db.get(AutomationJob, job_id)
            if failure_class == JobFailureClass.SESSION_EXPIRED:
                _mark_workspace_session_expired(workspace_id, message)
            mark_job_failed(db, job, message=message, failure_class=failure_class)
        return
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_succeeded(db, job, result)


async def _process_group_send(job_id: UUID) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_running(db, job)
        payload = dict(job.payload_json or {})
        workspace_id = job.workspace_id
    driver = await _driver_for_workspace(workspace_id)
    try:
        result = _normalize(await driver.send_group_message(payload.get("group_name", ""), payload.get("message", "")))
    except Exception as exc:
        message = str(exc)
        failure_class = _job_failure_class(exc)
        with session_scope() as db:
            job = db.get(AutomationJob, job_id)
            if failure_class == JobFailureClass.SESSION_EXPIRED:
                _mark_workspace_session_expired(workspace_id, message)
            mark_job_failed(db, job, message=message, failure_class=failure_class)
        return
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_succeeded(db, job, result)


async def _process_campaign_send(job_id: UUID) -> None:
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_running(db, job)
        payload = dict(job.payload_json or {})
        workspace_id = job.workspace_id
        campaign, contacts = prepare_campaign_job(db, workspace_id, int(payload["campaign_id"]))
        campaign_id = campaign.id
        campaign_message = campaign.message
        contacts_payload = [
            CampaignContactPreview(
                identity_key=item.identity_key,
                name=item.name,
                avatar_url=item.avatar_url,
                unread=item.unread,
                identity_source=item.identity_source,
                last_seen_at=item.last_seen_at.isoformat() if item.last_seen_at else None,
            ).model_dump()
            for item in contacts
        ]

    driver = await _driver_for_workspace(workspace_id)

    def progress_callback(event: dict[str, Any]) -> None:
        with session_scope() as db:
            progress_job = db.get(AutomationJob, job_id)
            if progress_job is None:
                return
            record_campaign_job_event(db, progress_job, event)
            renew_job_lease(db, progress_job)

    try:
        result = _normalize(
            await driver.send_campaign_messages(
                contacts_payload,
                campaign_message,
                payload.get("delay_min", 1.0),
                payload.get("delay_max", 3.0),
                progress_callback=progress_callback,
            )
        )
    except Exception as exc:
        message = str(exc)
        failure_class = _job_failure_class(exc)
        with session_scope() as db:
            job = db.get(AutomationJob, job_id)
            campaign = get_campaign(db, workspace_id, campaign_id)
            campaign.status = CampaignStatus.FAILED
            if failure_class == JobFailureClass.SESSION_EXPIRED:
                _mark_workspace_session_expired(workspace_id, message)
            mark_job_failed(db, job, message=message, failure_class=failure_class)
        return

    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        campaign = get_campaign(db, workspace_id, campaign_id)
        result["results"] = result.get("results", [])
        campaign_contacts = list(
            db.query(Contact).filter(
                Contact.workspace_id == workspace_id,
                Contact.identity_key.in_([item["identity_key"] for item in contacts_payload]),
            )
        )
        campaign_info = persist_campaign_execution(
            db,
            campaign=campaign,
            contacts=campaign_contacts,
            job=job,
            send_result=result,
        )
        mark_job_succeeded(db, job, {"campaign": _normalize(campaign_info.model_dump()), **result})


async def process_job(job_id: UUID, job_type: JobType) -> None:
    if job_type == JobType.CONTACT_SYNC:
        await _process_contact_sync(job_id)
        return
    if job_type == JobType.MANUAL_SEND:
        await _process_manual_send(job_id)
        return
    if job_type == JobType.FRIEND_REQUEST:
        await _process_friend_request(job_id)
        return
    if job_type == JobType.GROUP_SEND:
        await _process_group_send(job_id)
        return
    if job_type == JobType.CAMPAIGN_SEND:
        await _process_campaign_send(job_id)
        return
    with session_scope() as db:
        job = db.get(AutomationJob, job_id)
        mark_job_failed(db, job, message=f"Unsupported job type: {job_type}", failure_class=JobFailureClass.VALIDATION)


async def worker_loop() -> None:
    logger.info("Worker loop started.")
    while True:
        claimed_job: tuple[UUID, JobType] | None = None
        with session_scope() as db:
            heartbeat_worker(db)
            job = claim_next_job(db)
            if job is not None:
                if job.cancel_requested:
                    mark_job_cancelled(db, job, "Job was cancelled before execution.")
                else:
                    claimed_job = (job.id, job.type)

        if not claimed_job:
            await asyncio.sleep(settings.worker_poll_interval_seconds)
            continue

        job_id, job_type = claimed_job
        try:
            await process_job(job_id, job_type)
        except Exception as exc:
            logger.exception("Worker job %s crashed", job_id)
            with session_scope() as db:
                job = db.get(AutomationJob, job_id)
                if job and job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                    mark_job_failed(db, job, message=str(exc), failure_class=JobFailureClass.PERMANENT)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-16s  %(levelname)-7s  %(message)s",
    )
    asyncio.run(worker_loop())


if __name__ == "__main__":
    main()
