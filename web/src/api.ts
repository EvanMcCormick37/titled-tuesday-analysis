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

export type Market = {
  slug: string;
  label: string;
  n_values: number[];
  traded_ns: number[];
  pipeline_steps: string[];
  event_url_example: string;
  strategies: Strategy[];
};

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
  latest_event: string;
  pipeline_missing: boolean;
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
  research: 'Research attendance',
};

export type ClashKind = 'hard' | 'adjacent' | 'multi_day';
export type Policy = 'auto' | 'ignore' | 'nudge_only' | 'cut' | 'override';

export type LeadRound = {
  name: string | null;
  starts_at: string;
  clash: 'hard' | 'adjacent' | 'mid_event';
  overlap_minutes?: number;
  gap_minutes?: number;
};

export type Lead = {
  event_id: number | null;
  event_name: string;
  event_url: string | null;
  source: string;
  tier: number | null;
  fide_tc: string | null;
  kind: ClashKind;
  rounds: LeadRound[];
  policy: Policy;
  policy_value: number | null;
  player_name: string | null;
  username: string | null;
  fair_value: number | null;
  p_participate: number | null;
  match: 'exact' | 'strong' | 'partial' | 'hint' | null;
  matched_as: string | null;
};

export type ResearchRun = {
  id: number;
  tourn_date: string;
  job_id: number | null;
  status: 'running' | 'leads' | 'no_leads' | 'failed';
  leads?: Lead[];
  stats: Record<string, unknown> | null;
  collector_errors: Record<string, string> | null;
  error: string | null;
  started_at: string;
  ended_at: string | null;
  candidate_count?: number;
  for_current_event?: boolean;
};

export type ResearchEvent = {
  id: number;
  source: string;
  name: string;
  url: string | null;
  tier: number | null;
  time_zone: string | null;
  fide_tc: string | null;
  location: string | null;
  start_date: string | null;
  end_date: string | null;
  players_hint: string | null;
  player_count: number;
  players?: string[];
  policy: Policy;
  policy_value: number | null;
  policy_note: string | null;
  last_seen_at: string;
  clash: { kind: ClashKind; rounds: LeadRound[] } | null;
};

export type ResearchOverview = {
  event_date: string;
  window: { start: string; end: string };
  run: ResearchRun | null;
  recent_runs: ResearchRun[];
  events: ResearchEvent[];
};
