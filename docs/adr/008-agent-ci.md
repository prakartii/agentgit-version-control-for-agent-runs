# ADR-008: Agent CI replays committed bundles against the real agent

Status: accepted

## Context

An agent's behaviour can regress without any test noticing: a prompt tweak, a
refactor of the loop, a changed tool schema. Phase 6 turns recordings into
regression tests that run on every pull request, without calling a model or a
real tool server (fast, free, deterministic, safe).

## Decision

1. **A scenario is a TOML file plus a committed bundle.** The bundle (`core/bundle.py`) is one JSON file holding the run, its ancestors,
   and every step and blob. Import recomputes each object's content hash and
   rejects a mismatch, so a bundle cannot carry altered content under a known id.
   Export is deterministic (sorted), so a re-export of the same run is a clean diff.
2. **The agent under test is a real subprocess.** The scenario gives its command,
   with `{server}` standing for the replay server command and `{python}` for the
   interpreter. Nothing about the agent is mocked, so the test exercises the code
   that ships. Model outputs come from a committed response cache in `replay`
   mode (ADR-005), so a cache miss is an error, never a live model call.
3. **Pass means all of:** the agent exits with the expected code, replay reports
   no divergence (unless `allow_divergence`), and any `stdout_contains` strings
   appear. Replay divergence is the regression signal: the agent now makes a
   request the recording never saw, or stops making one it did.
4. **Strict scenario files.** Unknown keys are errors (a typo must not silently
   weaken a check); a command without `{server}` is rejected.
5. **Outputs for CI:** exit code (0 pass, 1 fail, 2 bad scenario), Markdown for
   job summaries, JUnit XML for CI dashboards. The composite `action.yml` runs
   the command, writes the summary even on failure, and uploads the reports.
6. **Hermetic per scenario:** a fresh temporary object store, a timeout, and no
   index or database.

## Consequences

- Updating a recording is an explicit, reviewable commit (re-record, `ci add`,
  commit the new bundle), which is how an intended behaviour change is accepted.
- A scenario proves the agent still behaves the same *given those recorded tool
  results and model outputs*. It does not prove the agent is good, and it cannot
  detect a changed live model; that needs live evaluation, which is not this.
- The Action installs with `uv sync --locked` by default; other repositories
  override `install`. Posting PR comments is left out: the job summary needs no
  extra token permissions.
- Re-recording is not byte-reproducible (run timings are stored), so regeneration
  produces a new bundle even when behaviour is unchanged.
