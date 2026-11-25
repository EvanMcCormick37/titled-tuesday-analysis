import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
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
  if (status.pipeline_missing) {
    items.push(['bad', <>No pipeline run for {status.latest_event}. <button className="link" onClick={onGoPipeline}>Open pipeline</button></>]);
  } else if (!p) items.push(['neutral', 'No pipeline run yet.']);
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

// ── Collapsible cards ────────────────────────────────────────────────────────
// Every `.card` whose first child is (or directly contains) a heading collapses
// when that heading is clicked; everything after the first child hides.
// State is per page and heading text (up to any " · " suffix), kept in
// localStorage so it survives refreshes. Native <details> cards are left alone.

const COLLAPSE_KEY = 'tt.collapsedCards';

function readCollapsed(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? '[]')); } catch { return new Set(); }
}

function writeCollapsed(s: Set<string>) {
  try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...s])); } catch { /* storage unavailable */ }
}

function cardHeading(card: Element): HTMLElement | null {
  const first = card.firstElementChild;
  if (!first) return null;
  if (/^H[1-3]$/.test(first.tagName)) return first as HTMLElement;
  return first.querySelector<HTMLElement>(':scope > h1, :scope > h2, :scope > h3');
}

function cardKey(page: string, h: HTMLElement): string {
  const text = (h.firstChild?.textContent ?? h.textContent ?? '').split('·')[0].trim();
  return `${page}|${text}`;
}

export function useCollapsibleCards(root: RefObject<HTMLElement | null>, page: string) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const collapsed = readCollapsed();

    const sync = () => {
      el.querySelectorAll('.card:not(details)').forEach((card) => {
        const h = cardHeading(card);
        if (!h) return;
        const isCollapsed = collapsed.has(cardKey(page, h));
        if (!h.classList.contains('card-toggle')) {
          h.classList.add('card-toggle');
          h.setAttribute('role', 'button');
          h.tabIndex = 0;
        }
        if (card.hasAttribute('data-collapsed') !== isCollapsed) card.toggleAttribute('data-collapsed', isCollapsed);
        if (h.getAttribute('aria-expanded') !== String(!isCollapsed)) h.setAttribute('aria-expanded', String(!isCollapsed));
      });
    };

    const toggle = (target: EventTarget | null) => {
      const t = target as HTMLElement | null;
      const h = t?.closest<HTMLElement>('.card-toggle');
      if (!h || !el.contains(h) || t?.closest('button, a, input, select, textarea, label')) return false;
      const key = cardKey(page, h);
      if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
      writeCollapsed(collapsed);
      sync();
      return true;
    };
    const onClick = (e: MouseEvent) => { toggle(e.target); };
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'Enter' || e.key === ' ') && (e.target as HTMLElement).classList?.contains('card-toggle')
          && toggle(e.target)) e.preventDefault();
    };

    sync();
    // Only childList: our own attribute writes never re-trigger the observer.
    const mo = new MutationObserver(sync);
    mo.observe(el, { childList: true, subtree: true });
    el.addEventListener('click', onClick);
    el.addEventListener('keydown', onKey);
    return () => { mo.disconnect(); el.removeEventListener('click', onClick); el.removeEventListener('keydown', onKey); };
  }, [root, page]);
}
