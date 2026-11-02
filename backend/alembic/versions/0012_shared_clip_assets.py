"""Allow an owned clip asset to appear in multiple delivery batches.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clip_export_items", sa.Column("asset_key", sa.String(64), nullable=True))
    op.create_index("ix_clip_export_items_asset_key", "clip_export_items", ["asset_key"])
    constraints = sa.inspect(op.get_bind()).get_unique_constraints("clip_export_items")
    for constraint in constraints:
        if constraint["column_names"] == ["file_id"]:
            op.drop_constraint(constraint["name"], "clip_export_items", type_="unique")
    op.create_unique_constraint("uq_clip_item_export_file", "clip_export_items", ["export_id", "file_id"])
    op.create_index("ix_clip_export_items_file_id", "clip_export_items", ["file_id"])


def downgrade():
    shared = op.get_bind().execute(sa.text(
        "SELECT file_id FROM clip_export_items GROUP BY file_id HAVING count(*) > 1 LIMIT 1")).first()
    if shared:
        raise RuntimeError("Shared clip assets exist; downgrade would lose delivery/authorization relationships")
    op.drop_index("ix_clip_export_items_asset_key", table_name="clip_export_items")
    op.drop_column("clip_export_items", "asset_key")
    op.drop_index("ix_clip_export_items_file_id", table_name="clip_export_items")
    op.drop_constraint("uq_clip_item_export_file", "clip_export_items", type_="unique")
    op.create_unique_constraint("clip_export_items_file_id_key", "clip_export_items", ["file_id"])
