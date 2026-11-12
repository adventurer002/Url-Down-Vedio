"""Give user history its own deletable records."""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_history_entries",
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    )
    op.execute(sa.text(
        "INSERT INTO task_history_entries (task_id) "
        "SELECT id FROM tasks WHERE history_hidden = false"
    ))
    op.drop_column("tasks", "history_hidden")


def downgrade():
    op.add_column("tasks", sa.Column("history_hidden", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(sa.text(
        "UPDATE tasks SET history_hidden = true "
        "WHERE id NOT IN (SELECT task_id FROM task_history_entries)"
    ))
    op.drop_table("task_history_entries")
