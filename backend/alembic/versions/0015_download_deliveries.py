"""Account-scoped, resumable download delivery sessions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("download_deliveries",
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_token", sa.Uuid()),
        sa.Column("active_until", sa.DateTime(timezone=True)),
        sa.Column("fallback_task_id", sa.Uuid(), sa.ForeignKey("tasks.id")),
        sa.Column("fallback_quote_id", sa.Uuid(), sa.ForeignKey("allowance_quotes.id")),
        sa.Column("charged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.String(64)))
    op.create_index("ix_download_deliveries_user_id", "download_deliveries", ["user_id"])
    op.create_index("ix_download_deliveries_expires_at", "download_deliveries", ["expires_at"])


def downgrade():
    op.drop_table("download_deliveries")
