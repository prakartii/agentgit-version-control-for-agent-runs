import Link from "next/link";
import { listRuns, type Run } from "@/lib/api";
import { ErrorBox } from "./error-box";

function StatusBadge({ run }: { run: Run }) {
  if (run.status === "ok" && !run.redacted) return <span className="badge">ok</span>;
  return (
    <span className="badge warn">
      {run.status !== "ok" ? run.status : "redacted"}
    </span>
  );
}

export default async function Home() {
  let runs: Run[];
  try {
    runs = await listRuns();
  } catch (e) {
    return <ErrorBox error={e} />;
  }
  if (runs.length === 0) {
    return <p className="muted">No runs yet. Record one with <code>agentgit record</code>.</p>;
  }
  return (
    <>
      <h1>Runs</h1>
      <table>
        <thead>
          <tr><th>Run</th><th>Steps</th><th>Status</th><th>Agent</th><th>Parent</th></tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td><Link href={`/runs/${r.id}`} className="mono">{r.id.slice(0, 12)}</Link></td>
              <td>{r.step_count}</td>
              <td><StatusBadge run={r} /></td>
              <td>{r.metadata.agent ?? <span className="muted">-</span>}</td>
              <td>
                {r.parents[0] ? (
                  <Link href={`/diff?a=${r.parents[0]}&b=${r.id}`}>diff vs parent</Link>
                ) : (
                  <span className="muted">-</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
