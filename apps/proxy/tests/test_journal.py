import json
from pathlib import Path

from agentgit_core.objects import Step, StepTiming
from agentgit_core.repo import Repository
from agentgit_core.store import MemoryStore
from agentgit_proxy.journal import Journal, recover, unrecovered_journals


def timing(n: int = 0) -> StepTiming:
    return StepTiming(started_at_ms=1000 + n, duration_ms=5)


def store_step(repo: Repository, name: str) -> str:
    return repo.put_step(Step(method="tools/call", tool_name=name))


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def test_graceful_close_removes_the_journal(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.open({"server": "s"})
    journal.step(0, "a" * 64, timing())
    assert journal.path.exists()
    journal.close("r" * 64)
    assert not journal.path.exists()
    assert unrecovered_journals(tmp_path) == []


def test_header_and_step_lines_are_written_as_json(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.open({"server": "s"})
    journal.step(3, "b" * 64, timing(3))
    journal.mark("redacted")
    journal.release()
    header, step, flag = lines(journal.path)
    assert header["type"] == "header" and header["metadata"] == {"server": "s"}
    assert step["seq"] == 3 and step["step"] == "b" * 64
    assert flag["type"] == "redacted"


def test_a_live_session_is_never_recovered(tmp_path: Path) -> None:
    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    journal.open({})
    journal.step(0, store_step(repo, "a"), timing())
    # The owner still holds its lock: recovery must leave the file alone.
    assert unrecovered_journals(tmp_path) == []
    assert recover(tmp_path, repo) == []
    assert journal.path.exists()
    journal.release()


def test_crashed_session_is_recovered_as_an_aborted_run(tmp_path: Path) -> None:
    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    journal.open({"server": "files", "transport": "stdio"})
    # Journaled out of order: recovery must restore request order.
    journal.step(1, store_step(repo, "second"), timing(1))
    journal.step(0, store_step(repo, "first"), timing(0))
    journal.meta("protocol_version", "2026-07-28")
    journal.mark("redacted")
    journal.mark("dropped")
    journal.release()  # the process "dies": lock gone, no closed marker

    assert unrecovered_journals(tmp_path) == [journal.path]
    (result,) = recover(tmp_path, repo)
    assert result.run_id is not None and result.steps == 2
    run = repo.get_run(result.run_id)
    assert [repo.get_step(s).tool_name for s in run.steps] == ["first", "second"]
    assert [t.started_at_ms for t in run.timings] == [1000, 1001]
    assert run.status == "aborted" and run.redacted is True and run.dropped_steps == 1
    assert run.metadata["recovered_from_journal"] == "true"
    assert run.metadata["server"] == "files" and run.metadata["protocol_version"] == "2026-07-28"
    assert not journal.path.exists()  # consumed
    assert recover(tmp_path, repo) == []  # and recovery is not repeated


def test_torn_last_line_from_the_kill_is_ignored(tmp_path: Path) -> None:
    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    journal.open({})
    journal.step(0, store_step(repo, "ok"), timing())
    journal.release()
    with journal.path.open("ab") as f:
        f.write(b'{"type":"step","seq":1,"ste')  # killed mid-write
    (result,) = recover(tmp_path, repo)
    assert result.steps == 1


def test_journaled_step_missing_from_the_store_is_counted_as_dropped(tmp_path: Path) -> None:
    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    journal.open({})
    journal.step(0, store_step(repo, "real"), timing())
    journal.step(1, "f" * 64, timing(1))  # never made it into the store
    journal.release()
    (result,) = recover(tmp_path, repo)
    run = repo.get_run(result.run_id)  # type: ignore[arg-type]
    assert len(run.steps) == 1 and run.dropped_steps == 1


def test_journal_already_closed_is_not_turned_into_a_second_run(tmp_path: Path) -> None:
    repo = Repository(MemoryStore())
    journal = Journal(tmp_path)
    journal.open({})
    journal.step(0, store_step(repo, "a"), timing())
    journal._write({"type": "closed", "run": "c" * 64})  # died between marker and delete
    journal.release()
    (result,) = recover(tmp_path, repo)
    assert result.run_id == "c" * 64 and result.note == "already closed"
    assert not journal.path.exists()


def test_journal_is_kept_when_the_recovered_run_cannot_be_stored(tmp_path: Path) -> None:
    class NoRuns(MemoryStore):
        def put(self, object_type: str, payload: bytes) -> str:
            if object_type == "run":
                raise OSError("disk full")
            return super().put(object_type, payload)

    repo = Repository(NoRuns())
    journal = Journal(tmp_path)
    journal.open({})
    journal.step(0, store_step(repo, "a"), timing())
    journal.release()
    (result,) = recover(tmp_path, repo)
    assert result.run_id is None
    assert journal.path.exists()  # nothing is lost; try again later


def test_unwritable_journal_directory_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    journal = Journal(blocker / "journal")  # parent is a file: mkdir will fail
    journal.open({})
    journal.step(0, "a" * 64, timing())
    journal.close("r" * 64)  # every call is a safe no-op
