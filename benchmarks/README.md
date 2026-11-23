# Benchmarks

`python benchmarks/bench.py [proxy] [store] [replay]` measures AgentGit's own
costs and writes `benchmarks/results/latest.json` (ignored by git). The file
`results/windows-2026-11-23.json` is one real run, committed as a reference.

## One run, on one machine

Windows 11, Python 3.12.3, 20 logical CPUs, nothing else deliberately running.
A single run each, so treat the figures as rough.

| What | Result |
|---|---|
| Round trip, direct to a do-nothing server | median 0.16 ms, p95 0.29 ms (n=1000) |
| Round trip, through `agentgit record` | median 2.79 ms, p95 14.3 ms, p99 23.7 ms (n=1000) |
| Cost the proxy adds per call (median) | about 2.6 ms |
| Object store, new objects written | 218 per second |
| Object store, writing objects that already exist | 9185 per second |
| Object store, reads | 1853 per second |
| Replay engine, in-process, 2000 recorded steps | 1346 requests per second |

## How to read this

- The proxy's added latency is small next to a model call (hundreds of ms to
  seconds) but is not free, and its tail (p95/p99) is much worse than its median.
  The measured loop uses a server that does no work, so this is the worst case
  for relative overhead.
- Writing new objects is slow because each is written to a temporary file and
  moved into place so a crash cannot leave a half-written object. Re-writing
  existing content is fast because the object is detected and skipped, which is
  the common case for repeated tool results.
- Replay reads each step and its blobs from disk. There is no cache yet.

## What is not measured

- Linux or macOS (process pipes are notably slower on Windows).
- Postgres indexing, the HTTP proxy, concurrent sessions, large payloads.
- Anything with a real model. Cost and latency of live model calls dominate real
  agent runs and depend on the provider.
