"""Index service: write runs into Postgres and query them.

The object store stays the source of truth (ADR-002). ``index_run`` reads a
run back out of the repository and inserts rows; it is idempotent, so
re-indexing the same run is safe.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from agentgit_core.repo import Repository
from agentgit_index.models import RefRow, RunParentRow, RunRow, RunStepRow, StepRow


@dataclass(frozen=True)
class RunSummary:
    id: str
    parents: tuple[str, ...]
    step_count: int
    metadata: dict[str, str]


class IndexService:
    def __init__(self, engine: Engine, repo: Repository) -> None:
        self._engine = engine
        self._repo = repo

    def index_run(self, run_id: str) -> None:
        """Index a run and, first, any parent runs it depends on."""
        with Session(self._engine) as session, session.begin():
            self._index(session, run_id)

    def _index(self, session: Session, run_id: str) -> None:
        run = self._repo.get_run(run_id)
        # Parents first so the foreign keys on run_parents are satisfied.
        for parent in run.parents:
            self._index(session, parent)
        session.execute(
            insert(RunRow)
            .values(id=run_id, fork_step=run.fork_step, run_metadata=run.metadata)
            .on_conflict_do_nothing()
        )
        for position, step_id in enumerate(run.steps):
            step = self._repo.get_step(step_id)
            session.execute(
                insert(StepRow)
                .values(
                    id=step_id,
                    kind=step.kind,
                    tool_name=step.tool_name,
                    is_error=step.is_error,
                    duration_ms=step.duration_ms,
                )
                .on_conflict_do_nothing()
            )
            session.execute(
                insert(RunStepRow)
                .values(run_id=run_id, position=position, step_id=step_id)
                .on_conflict_do_nothing()
            )
        for parent in run.parents:
            session.execute(
                insert(RunParentRow)
                .values(run_id=run_id, parent_id=parent)
                .on_conflict_do_nothing()
            )

    def set_ref(self, name: str, run_id: str) -> None:
        """Point ``name`` at ``run_id``, creating or moving it atomically."""
        with Session(self._engine) as session, session.begin():
            self._index(session, run_id)
            stmt = insert(RefRow).values(name=name, run_id=run_id)
            session.execute(
                stmt.on_conflict_do_update(index_elements=["name"], set_={"run_id": run_id})
            )

    def resolve_ref(self, name: str) -> str | None:
        with Session(self._engine) as session:
            return session.scalar(select(RefRow.run_id).where(RefRow.name == name))

    def resolve(self, name_or_prefix: str) -> str | None:
        """Resolve a ref name, full id or unique id prefix to a run id.

        Returns None if nothing matches. Raises ValueError if a prefix is
        ambiguous, as git does.
        """
        ref = self.resolve_ref(name_or_prefix)
        if ref is not None:
            return ref
        with Session(self._engine) as session:
            # Escape LIKE wildcards so user input is matched literally.
            escaped = (
                name_or_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            ids = session.scalars(
                select(RunRow.id).where(RunRow.id.like(escaped + "%", escape="\\")).limit(2)
            ).all()
        if len(ids) > 1:
            raise ValueError(f"ambiguous run id prefix: {name_or_prefix}")
        return ids[0] if ids else None

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        """Most recently indexed runs first (the data behind ``agentgit log``)."""
        with Session(self._engine) as session:
            runs = session.scalars(
                select(RunRow).order_by(RunRow.indexed_at.desc(), RunRow.id).limit(limit)
            ).all()
            return [self._summary(session, r) for r in runs]

    def get_run(self, run_id: str) -> RunSummary | None:
        with Session(self._engine) as session:
            row = session.get(RunRow, run_id)
            return self._summary(session, row) if row else None

    @staticmethod
    def _summary(session: Session, row: RunRow) -> RunSummary:
        parents = session.scalars(
            select(RunParentRow.parent_id).where(RunParentRow.run_id == row.id)
        ).all()
        steps = session.scalars(select(RunStepRow.step_id).where(RunStepRow.run_id == row.id)).all()
        return RunSummary(row.id, tuple(parents), len(steps), dict(row.run_metadata))
