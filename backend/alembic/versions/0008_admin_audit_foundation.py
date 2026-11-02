"""Administrative audit foundation; preserve all existing users and allowances.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column("actor_label", sa.String(128), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("changes", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("idempotency_key"))
    for name in ("action", "target_id", "created_at"):
        op.create_index(f"ix_audit_events_{name}", "audit_events", [name])


def downgrade():
    for name in ("created_at", "target_id", "action"):
        op.drop_index(f"ix_audit_events_{name}", table_name="audit_events")
    op.drop_table("audit_events")
