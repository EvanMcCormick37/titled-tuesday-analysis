import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import type { Job, JobStatus, Status } from './api';
import { ago, humanize } from './format';

const STATUS_DISPLAY: Record<JobStatus, [string, string, string]> = {
  queued: ['◷', 'Queued', 'neutral'],
  running: ['●', 'Running', 'info'],
  needs_review: ['!', 'Needs review', 'warn'],
  succeeded: ['✓', 'Succeeded', 'good'],
  partial: ['◐', 'Partial', 'warn'],
  failed: ['✕', 'Failed', 'bad'],
  cancelled: ['–', 'Cancelled', 'neutral'],
  interrupted: ['!', 'Interrupted', 'bad'],
  expired: ['–', 'Expired', 'neutral'],
};

export function StatusBadge({ job }: { job: Pick<Job, 'status' | 'run_at' | 'cancel_requested'> }) {
  let [icon, label, tone] = STATUS_DISPLAY[job.status];
  if (job.status === 'queued' && new Date(job.run_at).getTime() > Date.now()) {
    [icon, label] = ['◷', 'Scheduled'];
  }
  if (job.status === 'running' && job.cancel_requested) label = 'Stopping';
  return (
    <span className={`badge ${tone}${job.status === 'running' ? ' pulse' : ''}`}>
      <span aria-hidden="true">{icon}</span> {label}
    </span>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  return <p className="note bad" role="alert">{error instanceof Error ? error.message : String(error)}</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function Segmented<T extends string | number>({ label, value, options, onChange }: {
  label: string;
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map(([v, text]) => (
        <button key={String(v)} type="button" aria-pressed={v === value} onClick={() => onChange(v)}>{text}</button>
      ))}
    </div>
  );
}

export function Banner({ status, onGoPipeline }: { status: Status | undefined; onGoPipeline: () => void }) {
  if (!status) return null;
  const p = status.pipeline;
  const items: [string, ReactNode][] = [];
  if (!status.worker_online) {
    items.push(['bad', 'Trade worker is offline. Queued trades will not run until it is back.']);
  }
  if (!p) items.push(['neutral', 'No pipeline run yet.']);
  else if (p.state === 'failed') {
    items.push(['bad', <>Pipeline failed at {p.failed_step ? humanize(p.failed_step) : 'unknown step'}. <button className="link" onClick={onGoPipeline}>Open pipeline</button></>]);
  } else if (p.state === 'running') {
    items.push(['info', `Pipeline running: ${p.current_step ? humanize(p.current_step) : '…'}`]);
  } else if (p.state === 'adjustments_pending') {
    items.push(['warn', `Adjustments pending for ${status.event_date}. Trades are held until you recompute.`]);
  } else if (!status.trade_ready) {
    items.push(['warn', `Predictions for ${status.event_date} aren't marked complete. Trades are held for review.`]);
  }
  if (!items.length) {
    return <div className="banner good"><span aria-hidden="true">✓</span> Predictions ready for {status.event_date}{p?.completed_at ? ` · updated ${ago(p.completed_at)}` : ''}</div>;
  }
  return <>{items.map(([tone, content], i) => <div key={i} className={`banner ${tone}`} role="status">{content}</div>)}</>;
}

export function ConfirmDialog({ title, body, confirmLabel, busy, onConfirm, onCancel }: {
  title: string;
  body: string;
  confirmLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);
  return (
    <dialog ref={ref} className="confirm" onCancel={(e) => { e.preventDefault(); onCancel(); }}>
      <h2>{title}</h2>
      <p>{body}</p>
      <div className="row end">
        <button className="btn" onClick={onCancel} disabled={busy}>Go back</button>
        <button className="btn danger solid" onClick={onConfirm} disabled={busy}>{confirmLabel}</button>
      </div>
    </dialog>
  );
}

type Toast = { id: number; message: string; kind: 'ok' | 'error' };

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(0);
  const toast = useCallback((message: string, kind: 'ok' | 'error' = 'ok') => {
    const id = ++next.current;
    setToasts((t) => [...t, { id, message, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'error' ? 8000 : 4000);
  }, []);
  return { toasts, toast };
}

export function Toasts({ toasts }: { toasts: Toast[] }) {
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.message}</div>)}
    </div>
  );
}

export function DataAge({ fetchedAt, error, onRefresh, busy }: {
  fetchedAt: string | null | undefined;
  error?: string | null;
  onRefresh: () => void;
  busy?: boolean;
}) {
  return (
    <div className="row age">
      <span className="muted">{fetchedAt ? `Updated ${ago(fetchedAt)}` : 'Waiting for first update…'}</span>
      <button className="btn small" onClick={onRefresh} disabled={busy}>Refresh</button>
      {error && <span className="note bad inline">Last refresh failed: {error}</span>}
    </div>
  );
}
