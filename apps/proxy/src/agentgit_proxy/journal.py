"""Write-ahead journal so a hard-killed proxy does not lose its run.

Blobs and steps are stored as exchanges complete, but the run object that ties
them together is only written at the end. If the proxy is killed (SIGKILL,
TerminateProcess) that never happens. The journal closes the gap:

* One small file per session: a header line, then one JSON line per stored step
  (linear size, unlike writing a provisional run after every step).
* A graceful shutdown writes the run, appends a ``closed`` marker, and deletes
  the file. A file that survives therefore means "this session did not finish".
* A live proxy holds an OS lock on its journal; the lock vanishes with the
  process. ``recover`` takes the lock to prove the owner is dead before it
  touches the file, so it never steals a running session.

Durability: every line is one ``write`` call, so a killed process loses at most
a torn last line (ignored on recovery). Lines are not fsynced, so an operating
system crash or power loss can still lose recent steps.

What recovery cannot know: requests that were still in flight when the process
died never completed, so they were never journaled. The recovered run is
flagged ``aborted`` and ``recovered_from_journal``; it does not claim to be
complete.
"""

import contextlib
import json
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentgit_core.objects import Run, StepTiming
from agentgit_core.repo import Repository
from agentgit_index.service import IndexService

log = logging.getLogger("agentgit.proxy")

JOURNAL_SUFFIX = ".jsonl"
# The lock is taken far past the end of the content so that, on Windows, other
# processes can still read the bytes that hold data.
LOCK_OFFSET = 1 << 30
FORMAT_VERSION = 1


def _try_lock(fd: int) -> bool:
    """Take a non-blocking exclusive lock; False if another process holds it."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


_BINARY = getattr(os, "O_BINARY", 0)


class Journal:
    """Appends journal lines for one recording session. Never raises."""

    def __init__(self, directory: Path, name: str | None = None) -> None:
        self.path = directory / f"{name or uuid.uuid4().hex}{JOURNAL_SUFFIX}"
        self._fd: int | None = None
        self._lock = threading.Lock()  # appended to from the loop and a worker thread

    def open(self, metadata: dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | _BINARY, 0o600)
            if not _try_lock(fd):  # a fresh uuid name makes this practically impossible
                os.close(fd)
                raise OSError("journal is locked by another process")
            self._fd = fd
            self._write(
                {"type": "header", "v": FORMAT_VERSION, "pid": os.getpid(), "metadata": metadata}
            )
        except OSError:
            log.exception("could not open the journal; a hard kill would lose this session's run")
            self._fd = None

    def step(self, seq: int, step_id: str, timing: StepTiming) -> None:
        self._write(
            {
                "type": "step",
                "seq": seq,
                "step": step_id,
                "started_at_ms": timing.started_at_ms,
                "duration_ms": timing.duration_ms,
            }
        )

    def meta(self, key: str, value: str) -> None:
        self._write({"type": "meta", "key": key, "value": value})

    def mark(self, kind: Literal["redacted", "dropped"]) -> None:
        self._write({"type": kind})

    def close(self, run_id: str) -> None:
        """The run was stored: record that and remove the journal."""
        self._write({"type": "closed", "run": run_id})
        self.release()
        with contextlib.suppress(OSError):
            self.path.unlink()

    def release(self) -> None:
        """Drop the lock and file handle without a closed marker (simulates a crash)."""
        with self._lock:
            if self._fd is not None:
                _unlock(self._fd)
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    def _write(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, separators=(",", ":")).encode() + b"\n"
        with self._lock:
            if self._fd is None:
                return
            try:
                os.write(self._fd, line)
            except OSError:
                log.exception("journal write failed")


# ---- recovery ------------------------------------------------------------------


@dataclass(frozen=True)
class Recovery:
    journal: Path
    run_id: str | None  # None if the journal was unusable
    steps: int
    indexed: bool
    note: str = ""


def journals_in(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"*{JOURNAL_SUFFIX}")) if directory.is_dir() else []


def unrecovered_journals(directory: Path) -> list[Path]:
    """Journals whose owner is dead and that never finished (safe to recover)."""
    return [p for p in journals_in(directory) if _owner_is_dead(p)]


def _owner_is_dead(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDWR | _BINARY)
    except OSError:
        return False
    try:
        if not _try_lock(fd):
            return False
        _unlock(fd)
        return True
    finally:
        os.close(fd)


def recover(directory: Path, repo: Repository, index: IndexService | None = None) -> list[Recovery]:
    """Turn every dead session's journal into an ``aborted`` run, then delete it."""
    results = []
    for path in journals_in(directory):
        try:
            fd = os.open(path, os.O_RDWR | _BINARY)
        except OSError:
            continue
        result: Recovery | None = None
        try:
            if _try_lock(fd):  # otherwise its proxy is still running
                try:
                    result = _recover_one(path, repo, index)
                finally:
                    _unlock(fd)
        finally:
            os.close(fd)  # must be closed before the file can be deleted on Windows
        if result is not None:
            results.append(result)
            if result.run_id is not None:  # safely stored (or already was): drop the journal
                with contextlib.suppress(OSError):
                    path.unlink()
    return results


def _recover_one(path: Path, repo: Repository, index: IndexService | None) -> Recovery:
    metadata: dict[str, str] = {}
    steps: dict[int, tuple[str, StepTiming]] = {}
    redacted = False
    dropped = 0
    for raw in path.read_bytes().splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # a torn last line from the kill
        kind = entry.get("type")
        if kind == "closed":
            return Recovery(path, entry.get("run"), len(steps), True, "already closed")
        if kind == "header":
            metadata.update(entry.get("metadata", {}))
        elif kind == "meta":
            metadata[entry["key"]] = entry["value"]
        elif kind == "step":
            steps[entry["seq"]] = (
                entry["step"],
                StepTiming(started_at_ms=entry["started_at_ms"], duration_ms=entry["duration_ms"]),
            )
        elif kind == "redacted":
            redacted = True
        elif kind == "dropped":
            dropped += 1

    ordered = sorted(steps.items())
    present = [(seq, v) for seq, v in ordered if repo.has_step(v[0])]
    dropped += len(ordered) - len(present)  # a journaled step missing from the store
    metadata["recovered_from_journal"] = "true"
    run = Run(
        steps=tuple(v[0] for _, v in present),
        timings=tuple(v[1] for _, v in present),
        metadata=metadata,
        status="aborted",
        redacted=redacted,
        dropped_steps=dropped,
    )
    try:
        run_id = repo.put_run(run)
    except Exception as exc:
        log.exception("could not store the recovered run from %s", path)
        return Recovery(path, None, len(present), False, f"could not store run: {exc}")
    indexed = False
    if index is not None:
        try:
            index.index_run(run_id)
            indexed = True
        except Exception:
            log.exception("could not index recovered run %s", run_id)
    return Recovery(path, run_id, len(present), indexed)
