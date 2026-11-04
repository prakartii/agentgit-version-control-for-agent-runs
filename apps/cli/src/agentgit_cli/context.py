"""Builds the services the CLI commands need from environment settings.

``AGENTGIT_HOME`` is the directory holding the object store (default
``.agentgit``). ``DATABASE_URL`` points at the Postgres index.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine

from agentgit_core.repo import Repository
from agentgit_core.store import FilesystemStore
from agentgit_index.service import IndexService

DEFAULT_DB = "postgresql+psycopg://agentgit:agentgit@localhost:5433/agentgit"


@dataclass(frozen=True)
class Services:
    repo: Repository
    index: IndexService


def load_services() -> Services:
    home = Path(os.environ.get("AGENTGIT_HOME", ".agentgit"))
    repo = Repository(FilesystemStore(home / "objects"))
    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DB))
    return Services(repo=repo, index=IndexService(engine, repo))
