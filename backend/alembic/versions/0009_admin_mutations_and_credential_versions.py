"""Credential invalidation and replayable administrative operations.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False))
    for name in ("request_data", "result"):
        op.add_column("audit_events", sa.Column(name, sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                                              server_default="{}", nullable=False))


def downgrade():
    for name in ("result", "request_data"):
        op.drop_column("audit_events", name)
    op.drop_column("users", "auth_version")
