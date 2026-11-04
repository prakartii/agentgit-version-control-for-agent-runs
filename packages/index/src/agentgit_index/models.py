"""SQLAlchemy tables for the derived index (see ADR-002)."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fork_step: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="complete", server_default="complete")
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    dropped_steps: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    run_metadata: Mapped[dict[str, str]] = mapped_column("metadata", JSONB, default=dict)
    # Index insertion time, not part of the content-addressed run.
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RunParentRow(Base):
    """Edge in the run DAG: ``run_id`` was forked from ``parent_id``."""

    __tablename__ = "run_parents"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)


class StepRow(Base):
    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str | None] = mapped_column(String(32))  # legacy, see ADR-003
    method: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16), default="ok", server_default="ok")
    tool_name: Mapped[str | None] = mapped_column(String(256))
    is_error: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class RunStepRow(Base):
    """Ordered membership: step ``step_id`` is at ``position`` of ``run_id``."""

    __tablename__ = "run_steps"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"))
    # Timing lives on the run, not the step (ADR-003). Null for legacy rows.
    started_at_ms: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)


class RefRow(Base):
    __tablename__ = "refs"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
