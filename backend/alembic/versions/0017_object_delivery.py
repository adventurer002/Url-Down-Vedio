"""Record verified object mirrors and one-use transfer claims."""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_files", sa.Column("object_store_tag", sa.String(64)))
    op.add_column("media_files", sa.Column("object_attempted_at", sa.DateTime(timezone=True)))
    op.add_column("media_files", sa.Column("object_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("allowance_transfers", sa.Column("claim_expires_at", sa.DateTime(timezone=True)))
    op.add_column("allowance_transfers", sa.Column("claimed_at", sa.DateTime(timezone=True)))


def downgrade():
    active = op.get_bind().execute(sa.text(
        "SELECT 1 FROM allowance_transfers WHERE claim_expires_at IS NOT NULL AND sent IS NULL LIMIT 1"
    )).first()
    if active:
        raise RuntimeError("Object transfers have unsettled traffic reservations")
    for column in ("claimed_at", "claim_expires_at"):
        op.drop_column("allowance_transfers", column)
    for column in ("object_attempts", "object_attempted_at", "object_store_tag"):
        op.drop_column("media_files", column)
