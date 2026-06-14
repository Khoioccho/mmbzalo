"""initial schema

Revision ID: 20260531_000001
Revises:
Create Date: 2026-05-31 00:00:01
"""

from __future__ import annotations

from alembic import op

from app.database import Base
from app import db_models  # noqa: F401


revision = "20260531_000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
