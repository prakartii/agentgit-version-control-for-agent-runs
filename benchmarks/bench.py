"""Measure AgentGit's own costs on this machine. Prints a table and writes JSON.

    python benchmarks/bench.py                 # everything
    python benchmarks/bench.py proxy store     # chosen parts

What is measured, and what is not:

* ``proxy``: round-trip time of sequential tool calls to a do-nothing server,
  directly and through ``agentgit record``. The difference is the proxy's cost per
  call. Recording is written when the session ends, outside the measured loop.
* ``store``: object puts and gets on the filesystem store (no Postgres).
* ``replay``: requests per second the replay engine answers in-process.

Numbers depend on the machine, the OS (process pipes are slow on Windows) and what
else is running. Compare them only with runs from the same machine.
"""

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
REQUEST = (
    b'{"jsonrpc":"2.0","id":%d,"method":"tools/call",'
    b'"params":{"name":"echo","arguments":{"text":"hello"}}}\n'
)


def percentile(sorted_values: list[float], p: float) -> float:
    index = min(len(sorted_values) - 1, int(len(sorted_values) * p))
    return sorted_values[index]


def summarise(samples_s: list[float]) -> dict[str, float]:
    ms = sorted(s * 1000 for s in samples_s)
    return {
        "n": len(ms),
        "median_ms": round(statistics.median(ms), 3),
        "p95_ms": round(percentile(ms, 0.95), 3),
        "p99_ms": round(percentile(ms, 0.99), 3),
    }


def round_trips(command: list[str], env: dict[str, str], n: int, warmup: int) -> list[float]:
    """Send n sequential requests and time each reply."""
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )
    assert proc.stdin and proc.stdout
    samples: list[float] = []
    try:
        for i in range(warmup + n):
            start = time.perf_counter()
            proc.stdin.write(REQUEST % i)
            proc.stdin.flush()
            proc.stdout.readline()
            if i >= warmup:
                samples.append(time.perf_counter() - start)
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)
    return samples


def bench_proxy(n: int) -> dict[str, object]:
    server = [sys.executable, str(HERE / "echo_server.py")]
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "AGENTGIT_HOME": home}
        direct = round_trips(server, env, n, warmup=100)
        proxied = round_trips(
            [sys.executable, "-m", "agentgit_cli", "record", "--no-index", "--"] + server,
            env,
            n,
            warmup=100,
        )
    d, p = summarise(direct), summarise(proxied)
    return {
        "direct": d,
        "through_proxy": p,
        "added_median_ms": round(p["median_ms"] - d["median_ms"], 3),
    }


def timed(fn: Callable[[], None], repeats: int = 3) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def bench_store(count: int) -> dict[str, object]:
    from agentgit_core.store import FilesystemStore

    payloads = [json.dumps({"i": i, "text": "x" * 200}).encode() for i in range(count)]
    with tempfile.TemporaryDirectory() as d:
        store = FilesystemStore(Path(d))
        ids: list[str] = []

        def put_all() -> None:
            ids.clear()
            ids.extend(store.put("blob", p) for p in payloads)

        put_s = timed(put_all, repeats=1)  # first write: every object is new
        rewrite_s = timed(put_all)  # identical content: objects already exist
        get_s = timed(lambda: [store.get("blob", i) for i in ids])
    return {
        "objects": count,
        "put_new_per_s": round(count / put_s),
        "put_existing_per_s": round(count / rewrite_s),
        "get_per_s": round(count / get_s),
    }


def bench_replay(steps: int) -> dict[str, object]:
    from agentgit_core.objects import Run, Step
    from agentgit_core.repo import Repository
    from agentgit_core.store import FilesystemStore
    from agentgit_replay.engine import ReplayEngine

    with tempfile.TemporaryDirectory() as d:
        repo = Repository(FilesystemStore(Path(d)))
        ids = []
        requests = []
        for i in range(steps):
            args = {"text": f"call {i}"}
            step = Step(
                method="tools/call",
                tool_name="echo",
                arguments=repo.put_blob(json.dumps(args).encode()),
                result=repo.put_blob(b'{"content":[{"type":"text","text":"ok"}]}'),
            )
            ids.append(repo.put_step(step))
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": args},
                }
            )
        run_id = repo.put_run(Run(steps=tuple(ids)))

        def serve() -> None:
            engine = ReplayEngine(repo, run_id)
            for request in requests:
                engine.handle(request)

        elapsed = timed(serve)
    return {"recorded_steps": steps, "requests_per_s": round(steps / elapsed)}


def main() -> None:
    wanted = set(sys.argv[1:]) or {"proxy", "store", "replay"}
    results: dict[str, object] = {
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        }
    }
    if "store" in wanted:
        results["store"] = bench_store(2000)
    if "replay" in wanted:
        results["replay"] = bench_replay(2000)
    if "proxy" in wanted:
        results["proxy"] = bench_proxy(1000)
    out = HERE / "results" / "latest.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
