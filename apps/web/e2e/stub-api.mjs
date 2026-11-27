// A tiny fixed-data stand-in for the AgentGit API, so UI tests need no database.
// It serves one run, one fork of it, and the stats and diff between them.
import { createServer } from "node:http";

const A = "a".repeat(64);
const B = "b".repeat(64);

const step = (position, tool, ms, outcome = "ok") => ({
  position, method: "tools/call", tool_name: tool, outcome, started_at_ms: 1000 + position, duration_ms: ms,
});
const run = (id, parents, steps, extra = {}) => ({
  id, parents, step_count: steps.length, metadata: { agent: "demo-agent" },
  status: "complete", redacted: false, dropped_steps: 0, fork_step: null, steps, ...extra,
});

const runs = {
  [A]: run(A, [], [step(0, "read_file", 12), step(1, "write_file", 30)]),
  [B]: run(B, [A], [step(0, "read_file", 12), step(1, "write_file", 55)], { fork_step: 1 }),
};
const full = (id, pos) => ({
  ...runs[id].steps[pos], origin: "client", events: null,
  arguments: { path: "notes.txt" }, result: { content: [{ type: "text", text: `result of step ${pos}` }] },
});
const stats = (id) => ({
  run_id: id, steps: 2, timed_steps: 2, total_ms: runs[id].steps.reduce((n, s) => n + s.duration_ms, 0), errors: 0,
  tools: runs[id].steps.map((s) => ({ tool: s.tool_name, calls: 1, errors: 0, mean_ms: s.duration_ms, max_ms: s.duration_ms, total_ms: s.duration_ms })),
});
const diff = {
  a: A, b: B, identical: false, common_prefix: 1, first_divergence: 1,
  steps: [{ op: "replace", a_index: 1, b_index: 1, label: "write_file",
    changes: [{ path: "result.content[0].text", before: "saved", after: "EDITED" }] }],
};

const send = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};

createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://x");
  const parts = url.pathname.split("/").filter(Boolean);
  if (url.pathname === "/health") return send(res, 200, { status: "ok" });
  if (url.pathname === "/runs") return send(res, 200, Object.values(runs).map(({ steps, fork_step, ...r }) => r));
  if (url.pathname === "/diff") return send(res, 200, diff);
  if (parts[0] === "runs" && runs[parts[1]]) {
    if (parts[2] === "stats") return send(res, 200, stats(parts[1]));
    if (parts[2] === "steps") return send(res, 200, full(parts[1], Number(parts[3])));
    return send(res, 200, runs[parts[1]]);
  }
  send(res, 404, { detail: `no run matches '${parts[1] ?? ""}'` });
}).listen(4010);
