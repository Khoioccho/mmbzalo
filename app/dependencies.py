from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.db_models import MembershipRole, User
from app.services import require_workspace_membership, resolve_auth_session


@dataclass
class RequestContext:
    user: User
    auth_session_id: str
    active_workspace_id: object
    role: MembershipRole


def get_request_context(
    request: Request,
    db: Session = Depends(get_db),
    session_cookie: str | None = Cookie(default=None),
) -> RequestContext:
    settings = get_settings()
    raw_token = session_cookie or request.cookies.get(settings.session_cookie_name)
    resolved = resolve_auth_session(db, raw_token)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    auth_session, user = resolved
    membership = require_workspace_membership(db, user, auth_session.active_workspace_id)
    return RequestContext(
        user=user,
        auth_session_id=str(auth_session.id),
        active_workspace_id=auth_session.active_workspace_id,
        role=membership.role,
    )


def require_role(required_role: MembershipRole):
    def dependency(
        request: Request,
        db: Session = Depends(get_db),
        session_cookie: str | None = Cookie(default=None),
    ) -> RequestContext:
        settings = get_settings()
        raw_token = session_cookie or request.cookies.get(settings.session_cookie_name)
        resolved = resolve_auth_session(db, raw_token)
        if not resolved:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
        auth_session, user = resolved
        membership = require_workspace_membership(db, user, auth_session.active_workspace_id, required_role)
        return RequestContext(
            user=user,
            auth_session_id=str(auth_session.id),
            active_workspace_id=auth_session.active_workspace_id,
            role=membership.role,
        )

    return dependency
