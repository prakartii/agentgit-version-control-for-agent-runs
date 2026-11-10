# ADR-010: A read-only web UI over the existing API

Status: accepted

## Context

Runs are easiest to understand visually: a step list with payloads, and a diff
between a run and its fork. The CLI already does this in text. The UI should
not add a second way to change data.

## Decision

- **Next.js (App Router, TypeScript), server components.** Pages fetch from the
  FastAPI service on the server, so the browser never needs the API address and
  CORS is not needed in production. (CORS for `http://localhost:3000`, GET only,
  is still enabled for people who call the API directly from a browser.)
- **Read-only.** Three pages: runs list, run detail (expandable steps with
  decoded arguments and results), and diff (the same path-level changes as
  `agentgit diff`). Writing (record, fork) stays in the CLI.
- **The API gains two endpoints**, `GET /runs/{run}/steps/{n}` and
  `GET /diff?a=&b=`. The diff endpoint calls the same `diff_runs` function as the
  CLI, so the two cannot disagree.
- **Plain CSS, no component library.** Fewer dependencies to maintain; light and
  dark themes come from `prefers-color-scheme`.

## Consequences

- The run page fetches one request per step. That is fine for runs of tens of
  steps and would need a batch endpoint for thousands.
- No pagination in the UI yet (the list shows the latest 100 runs).
- Payloads are shown as recorded. They were already redacted at record time.
- Tested by type-checking and building in CI, and checked by hand against a real
  API. There are no browser tests yet.
