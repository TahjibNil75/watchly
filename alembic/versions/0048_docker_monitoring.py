"""Docker monitoring: Docker projects, their hosts and containers

- `docker` joins the project types. A Docker project's hosts each run the
  Watchly Docker agent, which pushes to `POST /api/v1/docker/ingest` with
  the host's token (kept as a hash).
- `docker_containers` holds each container's latest state, by name on its
  host; `docker_samples` its resource use per heartbeat, rolled up into
  `docker_sample_hourly`; `docker_events` the feed.

Revision ID: 0048_docker_monitoring
Revises: 0047_capacity_alerts
Create Date: 2026-10-07

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_docker_monitoring"
down_revision: str | None = "0047_capacity_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A value added to an enum cannot be used in the transaction that adds it;
    # nothing below does.
    op.execute("ALTER TYPE project_monitors ADD VALUE IF NOT EXISTS 'docker'")

    op.create_table(
        "docker_hosts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("down_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("down_alerted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("ignore_patterns", postgresql.ARRAY(sa.String(length=255)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("os", sa.String(length=255), nullable=True),
        sa.Column("kernel", sa.String(length=255), nullable=True),
        sa.Column("arch", sa.String(length=32), nullable=True),
        sa.Column("docker_version", sa.String(length=64), nullable=True),
        sa.Column("cpus", sa.Integer(), nullable=True),
        sa.Column("mem_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("containers_total", sa.Integer(), nullable=True),
        sa.Column("containers_running", sa.Integer(), nullable=True),
        sa.Column("agent_mem_bytes", sa.BigInteger(), nullable=True),
        sa.Column("docker_error", sa.Text(), nullable=True),
        sa.Column("dropped_events", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_docker_hosts_project_name"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_docker_hosts_project_id"), "docker_hosts", ["project_id"], unique=False)
    op.create_table(
        "docker_containers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("host_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("docker_id", sa.String(length=64), nullable=False),
        sa.Column("image", sa.String(length=1024), nullable=False),
        sa.Column("image_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("status_text", sa.String(length=255), nullable=True),
        sa.Column("health", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("restart_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("oom_killed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("docker_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compose_project", sa.String(length=255), nullable=True),
        sa.Column("compose_service", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("down_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("down_alerted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("muted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metrics_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["host_id"], ["docker_hosts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "name", name="uq_docker_containers_host_name"),
    )
    op.create_index(op.f("ix_docker_containers_host_id"), "docker_containers", ["host_id"], unique=False)
    op.create_index(op.f("ix_docker_containers_project_id"), "docker_containers", ["project_id"], unique=False)
    op.create_index(op.f("ix_docker_containers_removed_at"), "docker_containers", ["removed_at"], unique=False)
    op.create_table(
        "docker_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("host_id", sa.Integer(), nullable=False),
        sa.Column("container_id", sa.Integer(), nullable=True),
        sa.Column("container_name", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("downtime_seconds", sa.Integer(), nullable=True),
        sa.Column("event_key", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["container_id"], ["docker_containers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_id"], ["docker_hosts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "event_key", name="uq_docker_events_host_key"),
    )
    op.create_index(op.f("ix_docker_events_container_id"), "docker_events", ["container_id"], unique=False)
    op.create_index(op.f("ix_docker_events_occurred_at"), "docker_events", ["occurred_at"], unique=False)
    op.create_index(op.f("ix_docker_events_project_id"), "docker_events", ["project_id"], unique=False)
    op.create_table(
        "docker_sample_hourly",
        sa.Column("container_id", sa.Integer(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("cpu_avg", sa.Float(), nullable=True),
        sa.Column("cpu_max", sa.Float(), nullable=True),
        sa.Column("mem_avg", sa.Float(), nullable=True),
        sa.Column("mem_max", sa.BigInteger(), nullable=True),
        sa.Column("mem_pct_max", sa.Float(), nullable=True),
        sa.Column("net_rx_avg", sa.Float(), nullable=True),
        sa.Column("net_tx_avg", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["container_id"], ["docker_containers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("container_id", "hour"),
    )
    op.create_table(
        "docker_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("container_id", sa.Integer(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("mem_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mem_limit_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mem_pct", sa.Float(), nullable=True),
        sa.Column("net_rx_bps", sa.Float(), nullable=True),
        sa.Column("net_tx_bps", sa.Float(), nullable=True),
        sa.Column("blk_read_bps", sa.Float(), nullable=True),
        sa.Column("blk_write_bps", sa.Float(), nullable=True),
        sa.Column("pids", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["container_id"], ["docker_containers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_docker_samples_at"), "docker_samples", ["at"], unique=False)
    op.create_index("ix_docker_samples_container_at", "docker_samples", ["container_id", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_docker_samples_container_at", table_name="docker_samples")
    op.drop_index(op.f("ix_docker_samples_at"), table_name="docker_samples")
    op.drop_table("docker_samples")
    op.drop_table("docker_sample_hourly")
    op.drop_index(op.f("ix_docker_events_project_id"), table_name="docker_events")
    op.drop_index(op.f("ix_docker_events_occurred_at"), table_name="docker_events")
    op.drop_index(op.f("ix_docker_events_container_id"), table_name="docker_events")
    op.drop_table("docker_events")
    op.drop_index(op.f("ix_docker_containers_removed_at"), table_name="docker_containers")
    op.drop_index(op.f("ix_docker_containers_project_id"), table_name="docker_containers")
    op.drop_index(op.f("ix_docker_containers_host_id"), table_name="docker_containers")
    op.drop_table("docker_containers")
    op.drop_index(op.f("ix_docker_hosts_project_id"), table_name="docker_hosts")
    op.drop_table("docker_hosts")

    # Postgres cannot drop an enum value, and the older code cannot read a
    # project with it. The projects go; the value stays unused. Compared as
    # text, in case the value was added in this same transaction.
    op.execute("DELETE FROM projects WHERE monitors::text = 'docker'")
