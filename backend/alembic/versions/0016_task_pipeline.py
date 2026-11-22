"""Persist task handoffs, reusable checkpoints and download cache identities."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("phase", sa.String(24)))
    op.add_column("tasks", sa.Column("phase_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("tasks", sa.Column("prepared_transcript_id", sa.Uuid()))
    op.create_index("ix_tasks_phase_dispatch", "tasks", ["phase", "status", "last_dispatched_at"])
    op.create_table("task_checkpoints",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_task_checkpoints_expires_at", "task_checkpoints", ["expires_at"])
    op.add_column("media_files", sa.Column("download_key", sa.String(64)))
    op.create_index("ix_media_files_download_key", "media_files", ["download_key"])


def downgrade():
    active = op.get_bind().execute(sa.text(
        "SELECT 1 FROM tasks WHERE phase IS NOT NULL AND status IN ('queued', 'running', 'processing') LIMIT 1"
    )).first()
    if active:
        raise RuntimeError("Staged tasks are active; drain them before downgrade")
    op.drop_index("ix_media_files_download_key", table_name="media_files")
    op.drop_column("media_files", "download_key")
    op.drop_table("task_checkpoints")
    op.drop_index("ix_tasks_phase_dispatch", table_name="tasks")
    for column in ("prepared_transcript_id", "phase_attempts", "phase"):
        op.drop_column("tasks", column)
