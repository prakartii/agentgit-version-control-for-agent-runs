// Typed client for the AgentGit API. Pages are server components, so these
// calls run on the Next.js server and the browser never talks to the API.

export const API_URL = process.env.AGENTGIT_API_URL ?? "http://localhost:8000";
// Read on the server only (no NEXT_PUBLIC_ prefix), so it never reaches the browser.
const TOKEN = process.env.AGENTGIT_API_TOKEN;

export type Run = {
  id: string;
  parents: string[];
  step_count: number;
  metadata: Record<string, string>;
  status: string;
  redacted: boolean;
  dropped_steps: number;
};

export type Step = {
  position: number;
  method: string | null;
  tool_name: string | null;
  outcome: string;
  started_at_ms: number | null;
  duration_ms: number | null;
};

export type RunDetail = Run & { fork_step: number | null; steps: Step[] };

export type StepFull = Step & {
  origin: string;
  arguments: unknown;
  result: unknown;
  events: unknown;
};

export type Change = { path: string; before: unknown; after: unknown };

export type StepDiff = {
  op: "equal" | "replace" | "delete" | "insert";
  a_index: number | null;
  b_index: number | null;
  label: string;
  changes: Change[];
};

export type Diff = {
  a: string;
  b: string;
  identical: boolean;
  common_prefix: number;
  first_divergence: number | null;
  steps: StepDiff[];
};

export type ToolStats = {
  tool: string;
  calls: number;
  errors: number;
  mean_ms: number;
  max_ms: number;
  total_ms: number;
};

export type Stats = {
  run_id: string;
  steps: number;
  timed_steps: number;
  total_ms: number;
  errors: number;
  tools: ToolStats[];
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      cache: "no-store",
      headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
    });
  } catch {
    throw new ApiError(0, `Cannot reach the AgentGit API at ${API_URL}. Is it running?`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail ?? `API returned ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const listRuns = () => get<Run[]>("/runs?limit=100");
export const getRun = (id: string) => get<RunDetail>(`/runs/${encodeURIComponent(id)}`);
export const getStep = (id: string, position: number) =>
  get<StepFull>(`/runs/${encodeURIComponent(id)}/steps/${position}`);
export const getDiff = (a: string, b: string) =>
  get<Diff>(`/diff?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
export const getStats = (id: string) => get<Stats>(`/runs/${encodeURIComponent(id)}/stats`);
