"""Private highlight plans, exports and media provenance.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_files", sa.Column("purpose", sa.String(16), nullable=False, server_default="legacy"))
    op.add_column("media_files", sa.Column("source_sha256", sa.String(64)))
    op.add_column("media_files", sa.Column("source_start", sa.Float()))
    op.add_column("media_files", sa.Column("source_end", sa.Float()))
    op.create_check_constraint("ck_media_purpose", "media_files", "purpose IN ('legacy','full_source','full_audio','clip')")
    json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("clip_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), unique=True, nullable=False),
        sa.Column("video_id", sa.Uuid(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("transcript_id", sa.Uuid(), sa.ForeignKey("transcripts.id"), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", json, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "cache_key", name="uq_clip_plan_owner_key"))
    op.create_table("clip_timelines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("clip_plans.id"), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("content", json, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "cache_key", name="uq_clip_timeline_owner_key"))
    op.create_table("clip_exports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), unique=True, nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("clip_plans.id"), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("content", json, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("clip_export_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("export_id", sa.Uuid(), sa.ForeignKey("clip_exports.id"), nullable=False),
        sa.Column("file_id", sa.Uuid(), unique=True, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", json, nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_clip_item_position"))
    for table, columns in (("clip_plans", ("user_id", "video_id", "transcript_id")),
                           ("clip_timelines", ("user_id", "plan_id")),
                           ("clip_exports", ("user_id", "plan_id", "cache_key")),
                           ("clip_export_items", ("export_id",))):
        for name in columns:
            op.create_index(f"ix_{table}_{name}", table, [name])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM clip_plans LIMIT 1")).first():
        raise RuntimeError("Clip artifacts exist; retain schema when rolling back application")
    for table in ("clip_export_items", "clip_exports", "clip_timelines", "clip_plans"):
        op.drop_table(table)
    op.drop_constraint("ck_media_purpose", "media_files", type_="check")
    for name in ("source_end", "source_start", "source_sha256", "purpose"):
        op.drop_column("media_files", name)
