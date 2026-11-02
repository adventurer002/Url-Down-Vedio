"""Private media uploads and scoped AI caches.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("videos", sa.Column("source_kind", sa.String(16), server_default="link", nullable=False))
    op.add_column("videos", sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("videos", sa.Column("content_sha256", sa.String(64), nullable=True))
    op.add_column("videos", sa.Column("media_type", sa.String(16), server_default="video", nullable=False))
    for name, kind in (("platform", sa.String(64)), ("platform_video_id", sa.String(255)), ("source_url", sa.Text)):
        op.alter_column("videos", name, existing_type=kind, nullable=True)
    op.create_index("ix_videos_owner_id", "videos", ["owner_id"])
    op.create_index("uq_videos_private_content", "videos", ["owner_id", "content_sha256"], unique=True)
    op.create_check_constraint("ck_videos_source", "videos", "(source_kind = 'link' AND platform IS NOT NULL AND platform_video_id IS NOT NULL AND source_url IS NOT NULL AND owner_id IS NULL) OR (source_kind = 'upload' AND owner_id IS NOT NULL AND content_sha256 IS NOT NULL AND source_url IS NULL)")
    op.add_column("ai_results", sa.Column("cache_scope", sa.String(128), server_default="public", nullable=False))
    op.drop_index("uq_ai_results_cache", table_name="ai_results")
    op.create_index("uq_ai_results_cache", "ai_results", ["cache_scope", "source_content_hash", "result_type", "llm_model", "prompt_version", "output_language"], unique=True)
    op.create_table("uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("extension", sa.String(8), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("video_id", sa.Uuid(), sa.ForeignKey("videos.id")),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), unique=True),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("media_type", sa.String(16)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expected_bytes > 0 AND size_bytes >= 0", name="ck_uploads_size"),
        sa.CheckConstraint("status IN ('pending','uploaded','validating','ready','failed','expired','deleted')", name="ck_uploads_status"))
    for name in ("user_id", "status", "video_id", "created_at", "expires_at"):
        op.create_index("ix_uploads_" + name, "uploads", [name])


def downgrade():
    # A schema rollback cannot represent private sources or scoped caches safely.
    connection = op.get_bind()
    if (connection.execute(sa.text("SELECT 1 FROM uploads LIMIT 1")).first()
            or connection.execute(sa.text("SELECT 1 FROM videos WHERE source_kind = 'upload' LIMIT 1")).first()):
        raise RuntimeError("Private media exists; export it before downgrading 0010")
    op.drop_table("uploads")
    op.drop_index("uq_ai_results_cache", table_name="ai_results")
    op.create_index("uq_ai_results_cache", "ai_results", ["source_content_hash", "result_type", "llm_model", "prompt_version", "output_language"], unique=True)
    op.drop_column("ai_results", "cache_scope")
    op.drop_constraint("ck_videos_source", "videos", type_="check")
    op.drop_index("uq_videos_private_content", table_name="videos")
    op.drop_index("ix_videos_owner_id", table_name="videos")
    for name, kind in (("platform", sa.String(64)), ("platform_video_id", sa.String(255)), ("source_url", sa.Text)):
        op.alter_column("videos", name, existing_type=kind, nullable=False)
    for name in ("media_type", "content_sha256", "owner_id", "source_kind"):
        op.drop_column("videos", name)
