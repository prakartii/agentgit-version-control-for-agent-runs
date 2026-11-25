import Link from "next/link";
import { getRun, getStats, getStep, type RunDetail, type Stats, type StepFull } from "@/lib/api";
import { ErrorBox } from "../../error-box";

function Json({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="muted">none</span>;
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let run: RunDetail;
  let full: StepFull[];
  let stats: Stats;
  try {
    run = await getRun(id);
    stats = await getStats(id);
    // One request per step keeps the API small; runs are tens of steps, not thousands.
    full = await Promise.all(run.steps.map((s) => getStep(id, s.position)));
  } catch (e) {
    return <ErrorBox error={e} />;
  }
  return (
    <>
      <h1 className="mono">{run.id.slice(0, 16)}</h1>
      <p>
        {run.step_count} steps, status <span className="badge">{run.status}</span>
        {run.redacted && <span className="badge warn">redacted</span>}
        {run.dropped_steps > 0 && (
          <span className="badge bad">{run.dropped_steps} dropped</span>
        )}
      </p>
      {run.parents[0] && (
        <p>
          Forked from <Link href={`/runs/${run.parents[0]}`} className="mono">{run.parents[0].slice(0, 12)}</Link>
          {run.fork_step !== null && <> at step {run.fork_step}</>}
          {" - "}
          <Link href={`/diff?a=${run.parents[0]}&b=${run.id}`}>see the diff</Link>
        </p>
      )}
      <h2>Latency by tool</h2>
      <table>
        <thead>
          <tr><th>Tool</th><th>Calls</th><th>Errors</th><th>Mean ms</th><th>Max ms</th><th>Total ms</th></tr>
        </thead>
        <tbody>
          {stats.tools.map((t) => (
            <tr key={t.tool}>
              <td className="mono">{t.tool}</td><td>{t.calls}</td><td>{t.errors}</td>
              <td>{t.mean_ms}</td><td>{t.max_ms}</td><td>{t.total_ms}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {stats.timed_steps < stats.steps && (
        <p className="muted">Only {stats.timed_steps} of {stats.steps} steps have timings.</p>
      )}
      <h2>Steps</h2>
      {full.map((s) => (
        <details key={s.position}>
          <summary>
            <span className="mono">{s.position}</span>{" "}
            <strong>{s.tool_name ?? s.method}</strong>{" "}
            {s.outcome !== "ok" && <span className="badge bad">{s.outcome}</span>}
            {s.duration_ms !== null && <span className="muted"> {s.duration_ms} ms</span>}
          </summary>
          <p className="muted">{s.method} ({s.origin})</p>
          <strong>Arguments</strong>
          <Json value={s.arguments} />
          <strong>Result</strong>
          <Json value={s.result} />
        </details>
      ))}
    </>
  );
}
