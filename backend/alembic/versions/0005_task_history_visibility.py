"""Per-user task history visibility; retain jobs and results."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("history_hidden", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column("tasks", "history_hidden")
