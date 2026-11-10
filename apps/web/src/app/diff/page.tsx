import Link from "next/link";
import { getDiff, type Diff } from "@/lib/api";
import { ErrorBox } from "../error-box";

const SIGN = { equal: "=", replace: "~", delete: "-", insert: "+" } as const;

function show(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text === undefined ? "undefined" : text.length > 300 ? `${text.slice(0, 300)}...` : text;
}

export default async function DiffPage({
  searchParams,
}: {
  searchParams: Promise<{ a?: string; b?: string }>;
}) {
  const { a, b } = await searchParams;
  if (!a || !b) {
    return <p className="muted">Pick two runs: <code>/diff?a=RUN&amp;b=RUN</code></p>;
  }
  let diff: Diff;
  try {
    diff = await getDiff(a, b);
  } catch (e) {
    return <ErrorBox error={e} />;
  }
  return (
    <>
      <h1>Diff</h1>
      <p className="mono">
        <Link href={`/runs/${diff.a}`}>{diff.a.slice(0, 12)}</Link> to{" "}
        <Link href={`/runs/${diff.b}`}>{diff.b.slice(0, 12)}</Link>
      </p>
      {diff.identical ? (
        <p>Identical ({diff.common_prefix} shared steps).</p>
      ) : (
        <p>
          {diff.common_prefix} leading step(s) are the same objects; first difference at
          step {diff.first_divergence}.
        </p>
      )}
      {diff.steps.map((s, i) => (
        <div key={i} className="step-diff">
          <p>
            <span className={s.op === "insert" ? "add" : s.op === "delete" ? "del" : ""}>
              {SIGN[s.op]}
            </span>{" "}
            <span className="mono">[{s.a_index ?? "-"}/{s.b_index ?? "-"}]</span>{" "}
            <strong>{s.label}</strong>{" "}
            {s.op === "delete" && <span className="muted">only in A</span>}
            {s.op === "insert" && <span className="muted">only in B</span>}
          </p>
          {s.changes.map((c) => (
            <pre key={c.path}>
              {c.path}
              {"\n"}
              <span className="del">- {show(c.before)}</span>
              {"\n"}
              <span className="add">+ {show(c.after)}</span>
            </pre>
          ))}
        </div>
      ))}
    </>
  );
}
