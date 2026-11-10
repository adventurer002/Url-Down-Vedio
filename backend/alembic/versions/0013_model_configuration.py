"""Versioned model connections, encrypted credentials, releases and async probes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
J = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade():
    op.create_table(
        "model_resources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "model_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "resource_id",
            sa.Uuid(),
            sa.ForeignKey("model_resources.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", J, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("resource_id", "version", name="uq_model_revision_version"),
    )
    op.create_index(
        "ix_model_revisions_resource_id", "model_revisions", ["resource_id"]
    )
    op.create_table(
        "model_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "model_releases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("bindings", J, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "model_config_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Uuid(), sa.ForeignKey("model_releases.id")),
    )
    op.bulk_insert(
        sa.table(
            "model_config_state",
            sa.column("id", sa.Integer()),
            sa.column("version", sa.Integer()),
        ),
        [{"id": 1, "version": 0}],
    )
    op.create_table(
        "model_probes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("model_revisions.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id"), unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_model_probes_revision_id", "model_probes", ["revision_id"])

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("model_revisions.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_model_calls_revision_id", "model_calls", ["revision_id"])
    op.create_index("ix_model_calls_created_at", "model_calls", ["created_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM model_resources LIMIT 1")).first():
        raise RuntimeError(
            "Model configuration history exists; export and preserve before downgrade"
        )
    for table in (
        "model_calls",
        "model_probes",
        "model_config_state",
        "model_releases",
        "model_credentials",
        "model_revisions",
        "model_resources",
    ):
        op.drop_table(table)
