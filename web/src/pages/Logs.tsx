import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { req } from '../api';
import { Empty, ErrorNote, Segmented } from '../components';
import { humanize, time } from '../format';

export type LogRow = {
  id: number; pipeline_run_id: number | null; job_name: string; status: string;
  started_at: string | null; ended_at: string | null; summary: unknown; log_tail: string | null;
};

const TONE: Record<string, [string, string]> = {
  success: ['✓', 'good'], failed: ['✕', 'bad'], running: ['●', 'info'],
};

export function LogEntry({ title, row }: { title: string; row?: LogRow }) {
  if (!row) return <li className="log-entry"><span className="badge neutral">Not started</span> <span>{title}</span></li>;
  const [icon, tone] = TONE[row.status] ?? ['?', 'neutral'];
  return (
    <li className="log-entry">
      <details open={row.status === 'failed'}>
        <summary>
          <span className={`badge ${tone}`}><span aria-hidden="true">{icon}</span> {humanize(row.status)}</span>
          <strong>{title}</strong>
          <small className="muted">{time(row.started_at)}</small>
        </summary>
        {row.summary != null && <pre>{JSON.stringify(row.summary, null, 2)}</pre>}
        {row.log_tail && <pre>{row.log_tail}</pre>}
      </details>
    </li>
  );
}

export default function Logs() {
  const [limit, setLimit] = useState<number>(50);
  const q = useQuery({ queryKey: ['logs', limit], queryFn: () => req<LogRow[]>(`/logs?limit=${limit}`), refetchInterval: 10000 });
  return (
    <section className="card stack">
      <div className="row between wrap">
        <h2>Pipeline logs</h2>
        <Segmented label="Rows" value={limit} options={[[20, '20'], [50, '50'], [200, '200']]} onChange={setLimit} />
      </div>
      <p className="muted">Trade activity is on each job in the Trade queue.</p>
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.length === 0 && <Empty>No log entries yet.</Empty>}
      <ol className="steps">{q.data?.map((r) => <LogEntry key={r.id} title={humanize(r.job_name)} row={r} />)}</ol>
    </section>
  );
}
