import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ACTIVE, OPS, req, type Job, type JobEvent, type Plan } from './api';
import { useApp } from './App';
import { ErrorNote, Metric, StatusBadge } from './components';
import { clock, humanize, time, usd } from './format';

export function PlanTable({ plan }: { plan: Plan }) {
  return (
    <>
      {plan.orders.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>Top</th><th>Side</th><th className="num">Price</th><th className="num">Qty</th><th className="num">Cost</th><th className="num">ROI</th></tr></thead>
            <tbody>
              {plan.orders.map((o, i) => (
                <tr key={i} title={o.ticker}>
                  <td>{o.label || o.ticker}</td>
                  <td>{o.meta?.n ?? '—'}</td>
                  <td>{o.side.toUpperCase()}</td>
                  <td className="num">{o.price_c}¢</td>
                  <td className="num">{o.count}</td>
                  <td className="num">{usd(o.cost)}</td>
                  <td className="num">{o.meta?.roi?.toFixed(2) ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {plan.cancels.length > 0 && (
        <details open={plan.orders.length === 0}>
          <summary>{plan.cancels.length} order(s) to cancel</summary>
          <ul className="plain">
            {plan.cancels.map((c) => <li key={c.order_id}>{c.ticker} {c.side.toUpperCase()} {c.price_c !== null ? `@ ${c.price_c}¢` : ''}</li>)}
          </ul>
        </details>
      )}
      {plan.skipped.length > 0 && (
        <details><summary>{plan.skipped.length} skipped</summary>
          <ul className="plain">{plan.skipped.map((s, i) => <li key={i}>{s.ticker}: {s.reason}</li>)}</ul>
        </details>
      )}
      {plan.unmatched.length > 0 && (
        <details><summary>{plan.unmatched.length} unmatched player name(s)</summary>
          <ul className="plain">{plan.unmatched.map((n) => <li key={n}>{n}</li>)}</ul>
        </details>
      )}
    </>
  );
}

function useJob(id: number) {
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [error, setError] = useState<unknown>(null);
  const lastId = useRef(0);

  useEffect(() => {
    let active = true;
    let timer = 0;
    lastId.current = 0;
    setJob(null);
    setEvents([]);
    const poll = async () => {
      try {
        const data = await req<{ job: Job; events: JobEvent[] }>(`/jobs/${id}?after=${lastId.current}`);
        if (!active) return;
        setJob(data.job);
        setError(null);
        if (data.events.length) {
          lastId.current = data.events[data.events.length - 1].id;
          setEvents((prev) => [...prev, ...data.events]);
        }
        if (ACTIVE.includes(data.job.status)) timer = window.setTimeout(poll, 1500);
      } catch (e) {
        if (active) { setError(e); timer = window.setTimeout(poll, 4000); }
      }
    };
    poll();
    return () => { active = false; clearTimeout(timer); };
  }, [id]);

  return { job, events, error };
}

function jobTitle(job: Job, labelFor: (n: string) => string) {
  return OPS[job.strategy] ?? labelFor(job.strategy);
}

export function JobDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { market, toast } = useApp();
  const queryClient = useQueryClient();
  const { job, events, error } = useJob(id);
  const [cap, setCap] = useState('');
  const [override, setOverride] = useState(false);
  const labelFor = (n: string) => market.strategies.find((s) => s.name === n)?.label ?? humanize(n);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { ref.current?.showModal(); }, []);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['jobs'] });
  const onError = (e: unknown) => toast((e as Error).message, 'error');
  const cancel = useMutation({
    mutationFn: () => req(`/jobs/${id}/cancel`, 'POST'),
    onSuccess: () => { toast(`Job #${id}: cancel requested`); refresh(); },
    onError,
  });
  const approve = useMutation({
    mutationFn: () => req(`/jobs/${id}/approve`, 'POST', {
      max_exposure: cap ? Number(cap) : null, override_stale: override,
    }),
    onSuccess: () => { toast(`Job #${id} sent back to the queue`); refresh(); onClose(); },
    onError,
  });

  const planned = job?.plan?.exposure;
  const isOps = job ? job.strategy in OPS : false;
  const staleReview = job?.review_reason?.startsWith('Predictions');
  const summary = (job?.summary ?? {}) as Record<string, number | string>;

  return (
    <dialog ref={ref} className="drawer" onCancel={(e) => { e.preventDefault(); onClose(); }}
            onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="drawer-body">
        <header className="row between">
          <h2>{job ? `${jobTitle(job, labelFor)} #${job.id}` : `Job #${id}`}</h2>
          <button className="btn ghost" onClick={onClose} aria-label="Close">Close</button>
        </header>
        {error != null && <ErrorNote error={error} />}
        {job && (
          <>
            <div className="row wrap">
              <StatusBadge job={job} />
              {job.dry_run && <span className="badge neutral">Rehearsal</span>}
              {job.lane === 'priority' && <span className="badge bad">Kill switch</span>}
              {job.loop_interval_min !== null && <span className="badge neutral">Loop</span>}
            </div>

            {job.status === 'needs_review' && (
              <section className="note warn stack">
                <strong>Held for your review</strong>
                <span>{job.review_reason}</span>
                {staleReview ? (
                  <label className="check">
                    <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
                    Run on predictions that aren't marked complete
                  </label>
                ) : (
                  <label className="field">
                    <span>New max exposure ($)</span>
                    <input type="number" min="0" step="1" inputMode="decimal" value={cap}
                           placeholder={planned !== undefined ? String(planned) : ''}
                           onChange={(e) => setCap(e.target.value)} />
                  </label>
                )}
                <button className="btn primary"
                        disabled={approve.isPending || (staleReview ? !override : !cap)}
                        onClick={() => approve.mutate()}>Approve and re-queue</button>
              </section>
            )}

            {job.error && <p className="note bad">{job.error}</p>}

            {job.status === 'running' && job.progress_total > 0 && (
              <div className="progress" role="progressbar" aria-valuenow={job.progress_done} aria-valuemax={job.progress_total}>
                <div style={{ width: `${(job.progress_done / job.progress_total) * 100}%` }} />
                <span>{job.progress_done} / {job.progress_total}</span>
              </div>
            )}

            <div className="metrics">
              {!isOps && job.max_exposure !== null && <Metric label={job.loop_interval_min !== null ? 'Budget' : 'Max exposure'} value={usd(job.max_exposure)} />}
              {!isOps && <Metric label="Spent" value={usd(job.spent)} />}
              {'placed' in summary && <Metric label="Sent" value={summary.placed} />}
              {'cancelled' in summary && <Metric label="Cancelled" value={summary.cancelled} />}
              {'failed' in summary && <Metric label="Failed" value={summary.failed} />}
              {job.loop_interval_min !== null && <Metric label="Sweeps" value={job.iterations} />}
            </div>

            <dl className="facts">
              <dt>Event</dt><dd>{job.tourn_date}</dd>
              <dt>{job.status === 'queued' ? 'Runs at' : 'Started'}</dt><dd>{time(job.status === 'queued' ? job.run_at : job.started_at)}</dd>
              {job.not_after && <><dt>Expires</dt><dd>{time(job.not_after)}</dd></>}
              {job.ended_at && <><dt>Ended</dt><dd>{time(job.ended_at)}</dd></>}
              {typeof summary.stop_reason === 'string' && <><dt>Stopped</dt><dd>{humanize(summary.stop_reason)}</dd></>}
              <dt>From</dt><dd>{job.created_by ?? '—'}</dd>
            </dl>

            {ACTIVE.includes(job.status) && !job.cancel_requested && (
              <button className="btn danger" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                {job.status === 'running' ? 'Stop this job' : 'Remove from queue'}
              </button>
            )}

            {job.plan && (
              <section className="stack">
                <h3>{job.loop_interval_min !== null ? 'Latest sweep' : 'Orders'} · {usd(job.plan.exposure)}</h3>
                {job.plan.orders.length + job.plan.cancels.length === 0
                  ? <p className="muted">No orders in this plan.</p>
                  : <PlanTable plan={job.plan} />}
              </section>
            )}

            {Object.keys(job.params).length > 0 && (
              <details><summary>Settings</summary>
                <dl className="facts">
                  {Object.entries(job.params).map(([k, v]) => (
                    <div key={k} className="contents"><dt>{humanize(k)}</dt><dd>{v === null ? 'Any' : String(v)}</dd></div>
                  ))}
                </dl>
              </details>
            )}

            <section className="stack">
              <h3>Activity</h3>
              <ol className="events">
                {events.map((e) => (
                  <li key={e.id} className={e.level}>
                    <time>{clock(e.ts)}</time>
                    <span>{e.message}</span>
                    {(e.data?.tail || e.data?.traceback) && (
                      <details><summary>Details</summary><pre>{e.data.tail ?? e.data.traceback}</pre></details>
                    )}
                  </li>
                ))}
                {events.length === 0 && <li className="muted">Nothing yet.</li>}
              </ol>
            </section>
          </>
        )}
      </div>
    </dialog>
  );
}
