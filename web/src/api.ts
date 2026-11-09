export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function req<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export type SchemaProp = {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  anyOf?: { type?: string; enum?: string[] }[];
};

export type Strategy = {
  name: string;
  label: string;
  description: string;
  spends: boolean;
  can_loop: boolean;
  schema: { properties?: Record<string, SchemaProp> };
};

export type Market = { slug: string; label: string; n_values: number[]; traded_ns: number[]; strategies: Strategy[] };

export type PipelineRow = {
  id: number;
  tourn_date: string;
  state: string;
  current_step: string | null;
  failed_step: string | null;
  error_kind: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type Status = {
  event_date: string;
  event_start: string;
  trade_ready: boolean;
  pipeline: PipelineRow | null;
  worker_online: boolean;
};

export type OrderIntent = {
  kind: 'place' | 'take';
  ticker: string;
  side: string;
  count: number;
  price_c: number;
  label: string;
  cost: number;
  meta: Record<string, number>;
};

export type CancelIntent = {
  order_id: string;
  ticker: string;
  side: string;
  price_c: number | null;
  qty: number | null;
};

export type Plan = {
  strategy: string;
  tourn_date: string;
  exposure: number;
  orders: OrderIntent[];
  cancels: CancelIntent[];
  skipped: { ticker?: string; reason?: string }[];
  unmatched: string[];
  stats: Record<string, string | number>;
};

export type JobStatus =
  | 'queued' | 'running' | 'needs_review' | 'succeeded' | 'partial'
  | 'failed' | 'cancelled' | 'interrupted' | 'expired';

export type Job = {
  id: number;
  market: string;
  strategy: string;
  tourn_date: string;
  params: Record<string, unknown>;
  dry_run: boolean;
  lane: string;
  status: JobStatus;
  run_at: string;
  not_after: string | null;
  max_exposure: number | null;
  override_stale: boolean;
  loop_interval_min: number | null;
  loop_fallback_hours: number | null;
  iterations: number;
  spent: number;
  summary: Record<string, unknown> | null;
  progress_done: number;
  progress_total: number;
  review_reason: string | null;
  error: string | null;
  cancel_requested: boolean;
  created_by: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  plan?: Plan | null;
  plan_exposure?: number | null;
};

export type JobEvent = {
  id: number;
  ts: string;
  iteration: number;
  kind: string;
  level: string;
  ticker: string | null;
  message: string;
  data: { tail?: string; traceback?: string } | null;
};

export type Snapshot<T> = { payload: T | null; error: string | null; fetched_at: string | null };

export const ACTIVE: JobStatus[] = ['queued', 'running', 'needs_review'];
export const OPS: Record<string, string> = {
  recompute: 'Recompute predictions',
  run_pipeline: 'Run pipeline',
};
