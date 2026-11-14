import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { req } from '../api';
import { useApp } from '../App';
import { Empty, ErrorNote } from '../components';
import { humanize, time } from '../format';
import type { LogRow } from './Logs';
import { LogEntry } from './Logs';

type Run = { id: number; tourn_date: string; state: string; failed_step: string | null; error_kind: string | null; started_at: string | null; completed_at: string | null };
type Data = { runs: Run[]; steps: LogRow[] };

export default function Pipeline() {
  const { market, status, openJob, toast } = useApp();
  const queryClient = useQueryClient();
  const [url, setUrl] = useState('');
  useEffect(() => setUrl(''), [market.slug]);   // a URL belongs to one market
  const q = useQuery({
    queryKey: ['pipeline', market.slug],
    queryFn: () => req<Data>(`/markets/${market.slug}/pipeline`),
    refetchInterval: 5000,
  });
  const started = (r: { id: number }) => {
    openJob(r.id);
    queryClient.invalidateQueries({ queryKey: ['jobs'] });
  };
  const onError = (e: unknown) => toast((e as Error).message, 'error');
  const run = useMutation({
    mutationFn: (slug_url?: string) => req<{ id: number }>(`/markets/${market.slug}/pipeline/run`, 'POST', { slug_url: slug_url ?? null }),
    onSuccess: started, onError,
  });
  const recompute = useMutation({
    mutationFn: () => req<{ id: number }>(`/markets/${market.slug}/recompute`, 'POST'),
    onSuccess: started, onError,
  });

  const p = status?.pipeline;
  const canRun = !p || p.state === 'failed' || !!status?.pipeline_missing;
  const needsUrl = !status?.pipeline_missing && p?.state === 'failed' && p.error_kind === 'listing_missing_slug';
  const hasUrl = !!url.trim();
  const runLabel = status?.pipeline_missing
    ? `Run pipeline for ${status.latest_event}${hasUrl ? ' with this URL' : ''}`
    : hasUrl ? (p ? 'Retry with this URL' : 'Run with this URL')
    : p ? 'Retry pipeline' : 'Run pipeline';
  const latestByStep =new Map((q.data?.steps ?? []).map((s) => [s.job_name, s]));

  return (
    <div className="stack">
      <section className="card stack">
        <h2>Pipeline{p ? ` · ${humanize(p.state)}` : ''}</h2>
        {p?.state === 'failed' && p.error_message && <p className="note bad">{p.error_message}</p>}
        {status?.pipeline_missing && <p className="note bad">No pipeline run was recorded for {status.latest_event}. The scheduled run may not have started.</p>}
        {q.error && <ErrorNote error={q.error} />}
        {canRun && (
          <form className="stack" onSubmit={(e) => { e.preventDefault(); run.mutate(url.trim() || undefined); }}>
            <label className="stack">
              <span className="muted">
                {needsUrl
                  ? `Chess.com tournament URL for ${market.label} (required: the event wasn't found automatically)`
                  : `Chess.com tournament URL for ${market.label} (optional: leave blank to find the event automatically)`}
              </span>
              <input aria-label="Tournament URL" placeholder={market.event_url_example} value={url} onChange={(e) => setUrl(e.target.value)} />
            </label>
            <div className="row wrap">
              <button className="btn primary" disabled={run.isPending || (needsUrl && !url.trim())}>{runLabel}</button>
              <button type="button" className="btn" disabled={recompute.isPending} onClick={() => recompute.mutate()}>Recompute predictions</button>
            </div>
          </form>
        )}
        {!canRun && (
          <div className="row wrap">
            <button className="btn" disabled={recompute.isPending} onClick={() => recompute.mutate()}>Recompute predictions</button>
          </div>
        )}
        <h3>Steps</h3>
        <ol className="steps">
          {market.pipeline_steps.map((name) => {
            const s = latestByStep.get(name);
            return <LogEntry key={name} title={humanize(name)} row={s} />;
          })}
        </ol>
      </section>
      <section className="card stack">
        <h3>Recent runs</h3>
        {q.data && q.data.runs.length === 0 && <Empty>No runs yet.</Empty>}
        {q.data && q.data.runs.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Tournament</th><th>State</th><th>Failed step</th><th>Started</th><th>Completed</th></tr></thead>
              <tbody>{q.data.runs.map((r) => (
                <tr key={r.id}><td>{r.tourn_date}</td><td>{humanize(r.state)}</td><td>{r.failed_step ? humanize(r.failed_step) : '—'}</td><td>{time(r.started_at)}</td><td>{time(r.completed_at)}</td></tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
