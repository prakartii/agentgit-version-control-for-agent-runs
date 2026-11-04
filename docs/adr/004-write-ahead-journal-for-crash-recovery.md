# ADR-004: Write-ahead journal for crash recovery

Status: accepted

## Context

Blobs and steps are stored as exchanges complete, but a run is one immutable,
content-addressed object that lists all its steps, so it can only be written
at the end. A proxy killed without a chance to shut down (`SIGKILL`,
`TerminateProcess`, an agent host that kills MCP servers on exit) left stored
steps that no run referenced: the recording was silently lost.

## Decision

Each recording session keeps a small append-only journal file under
`<AGENTGIT_HOME>/journal/`:

- a header line (session metadata), then one JSON line per stored step
  (`seq`, step id, timing), plus `meta`, `redacted` and `dropped` lines;
- a step is journaled only after it is stored, so a journaled id always
  exists in the object store;
- a graceful shutdown writes the run, appends a `closed` marker and deletes
  the file, so a surviving file means "this session did not finish";
- if writing the run fails, the journal is kept for a later retry.

The live proxy holds an OS lock on its journal (`flock`, or `msvcrt.locking`
on a byte past the end of the data). The lock disappears with the process.
`agentgit recover` takes the lock to prove the owner is dead before it reads
the file, so it never steals a running session. It builds a run with
`status: aborted` and `recovered_from_journal`, stores it, indexes it
(best effort) and deletes the journal. `agentgit record` prints a note when
unrecovered journals exist.

## Alternatives rejected

- **Write a provisional run after every step.** Runs list all step ids, so
  the total size grows quadratically, and a mutable pointer to the latest one
  is still needed.
- **Process-id liveness checks.** `os.kill(pid, 0)` on Windows terminates the
  process; other approaches need extra dependencies, and pids are reused.
- **Recover automatically at startup.** It would race with other running
  proxies and change data behind the user's back; an explicit command plus a
  warning is predictable.
- **fsync every line.** It would add a disk flush to every step to survive
  power loss; the target failure is a killed process, which a plain `write`
  already survives.

## Consequences

- A killed process loses at most a torn last journal line, which recovery
  ignores. An operating-system crash or power loss can still lose recent steps.
- Requests still in flight when the process died never completed, so they were
  never journaled. A recovered run is flagged `aborted` and does not claim
  to contain them.
- Journal writes happen on the recording path; failures are logged and never
  reach the agent. A `dropped` mark is a small synchronous write when the queue
  overflows.
- Every session now creates one small file, deleted on clean exit.
