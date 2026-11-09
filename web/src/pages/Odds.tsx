import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { req } from '../api';
import { useApp } from '../App';
import { Empty, ErrorNote, Segmented } from '../components';
import { pct } from '../format';

type Row = { player: string; p_participate: number; p_given_play: number; p_top: number; yes_price: number; no_price: number };
type Data = { rows: Row[]; pool_size: number; tourn_date: string | null };

const N_OPTIONS = [[1, 'Winner'], [3, 'Top 3'], [5, 'Top 5'], [8, 'Top 8'], [10, 'Top 10']] as const;

function Bar({ value, max }: { value: number; max: number }) {
  return <div className="bar-track"><div className="bar" style={{ width: `${max > 0 ? (value / max) * 100 : 0}%` }} /></div>;
}

export default function Odds() {
  const { market } = useApp();
  const [n, setN] = useState<number>(1);
  const [adjusted, setAdjusted] = useState<'adjusted' | 'raw'>('adjusted');
  const [view, setView] = useState<'chances' | 'prices' | 'table'>('chances');

  const q = useQuery({
    queryKey: ['predictions', market.slug, n, adjusted],
    queryFn: () => req<Data>(`/markets/${market.slug}/predictions?n=${n}&adjusted=${adjusted === 'adjusted'}&limit=50`),
    placeholderData: keepPreviousData,
  });
  const rows = q.data?.rows ?? [];
  const label = N_OPTIONS.find(([v]) => v === n)![1];
  const maxP = Math.max(0, ...rows.map((r) => r.p_top));
  const maxYes = Math.max(0, ...rows.map((r) => r.yes_price));

  return (
    <section className="card stack">
      <div className="row between wrap">
        <h2>Odds · {label}</h2>
        <div className="row wrap">
          <Segmented label="Position" value={n} options={N_OPTIONS} onChange={setN} />
          <Segmented label="Source" value={adjusted} options={[['adjusted', 'Adjusted'], ['raw', 'Raw']]} onChange={setAdjusted} />
          <Segmented label="View" value={view} options={[['chances', 'Chances'], ['prices', 'Fair prices'], ['table', 'Table']]} onChange={setView} />
        </div>
      </div>
      {q.error && <ErrorNote error={q.error} />}
      {q.data && <p className="muted">{q.data.pool_size.toLocaleString()} players{q.data.tourn_date ? ` · ${q.data.tourn_date.slice(0, 10)}` : ''}</p>}
      {q.data && rows.length === 0 && <Empty>No predictions yet. Run the pipeline, then recompute.</Empty>}

      {view === 'chances' && rows.length > 0 && (
        <div className="chart" role="img" aria-label={`Chance of ${label} by player. Exact values are in the Table view.`}>
          {rows.slice(0, 30).map((r) => (
            <div key={r.player} className="chart-row" title={`P(plays) ${pct(r.p_participate)} · P(${label} | plays) ${pct(r.p_given_play)}`}>
              <span className="chart-name">{r.player}</span>
              <Bar value={r.p_top} max={maxP} />
              <span className="num">{pct(r.p_top)}</span>
            </div>
          ))}
        </div>
      )}

      {view === 'prices' && rows.length > 0 && (
        <div className="chart two">
          <div className="chart-row head"><span /><span>YES fair price</span><span /><span>NO fair price</span><span /></div>
          {rows.slice(0, 30).map((r) => (
            <div key={r.player} className="chart-row">
              <span className="chart-name">{r.player}</span>
              <Bar value={r.yes_price} max={maxYes} />
              <span className="num">{r.yes_price.toFixed(1)}¢</span>
              <Bar value={r.no_price} max={100} />
              <span className="num">{r.no_price.toFixed(1)}¢</span>
            </div>
          ))}
        </div>
      )}

      {view === 'table' && rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th className="num">P(plays)</th><th className="num">P(top | plays)</th><th className="num">P(top)</th><th className="num">YES ¢</th><th className="num">NO ¢</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.player}>
                  <td>{r.player}</td><td className="num">{pct(r.p_participate, 2)}</td>
                  <td className="num">{pct(r.p_given_play, 2)}</td><td className="num">{pct(r.p_top, 2)}</td>
                  <td className="num">{r.yes_price.toFixed(1)}</td><td className="num">{r.no_price.toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
