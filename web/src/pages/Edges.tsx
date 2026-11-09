import { useMutation, useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { req, type Snapshot } from '../api';
import { useApp } from '../App';
import { DataAge, Empty, ErrorNote, Metric, Segmented } from '../components';
import { usd } from '../format';

type Row = {
  ticker: string; player: string; n: number; side: string; ask: number; qty: number;
  ev: number; order_price: number; total_cost: number; expected_return: number; roi: number;
};
type Payload = { rows: Row[]; unmatched: string[] };
type SortKey = 'expected_return' | 'roi' | 'total_cost' | 'qty' | 'ev';

const COLUMNS: [SortKey, string][] = [['expected_return', 'Exp. return'], ['roi', 'ROI'], ['ev', 'EV'], ['qty', 'Qty'], ['total_cost', 'Cost']];
const NS = [1, 3, 5, 8];

export default function Edges() {
  const { market, status } = useApp();
  const [minRoi, setMinRoi] = useState(1.1);
  const [minQty, setMinQty] = useState(10);
  const [side, setSide] = useState<'All' | 'YES' | 'NO'>('All');
  const [ns, setNs] = useState<number[]>(NS);
  const [sort, setSort] = useState<SortKey>('expected_return');

  const path = `/markets/${market.slug}/snapshots/edges`;
  const q = useQuery({ queryKey: ['edges', market.slug], queryFn: () => req<Snapshot<Payload>>(path), refetchInterval: 4000 });
  const refresh = useMutation({ mutationFn: () => req<Snapshot<Payload>>(`${path}?force=true`) });

  const rows = useMemo(() => (q.data?.payload?.rows ?? [])
    .filter((r) => r.roi > minRoi && r.qty >= minQty && (side === 'All' || r.side === side) && ns.includes(r.n))
    .sort((a, b) => b[sort] - a[sort]), [q.data, minRoi, minQty, side, ns, sort]);

  const exposure = rows.reduce((s, r) => s + r.total_cost, 0);
  const expected = rows.reduce((s, r) => s + r.expected_return, 0);
  const unmatched = q.data?.payload?.unmatched ?? [];

  return (
    <section className="card stack">
      <div className="row between wrap">
        <h2>Edges</h2>
        <DataAge fetchedAt={q.data?.fetched_at} error={q.data?.error} onRefresh={() => refresh.mutate()} busy={refresh.isPending} />
      </div>
      {status && !status.trade_ready && <p className="note warn">Predictions aren't marked complete, so these values may be stale.</p>}
      {q.error && <ErrorNote error={q.error} />}
      <div className="row wrap">
        <label className="field compact"><span>Min ROI</span><input type="number" step="0.05" min="0" value={minRoi} onChange={(e) => setMinRoi(Number(e.target.value))} /></label>
        <label className="field compact"><span>Min qty</span><input type="number" step="5" min="1" value={minQty} onChange={(e) => setMinQty(Number(e.target.value))} /></label>
        <Segmented label="Side" value={side} options={[['All', 'All'], ['YES', 'YES'], ['NO', 'NO']]} onChange={setSide} />
        <div className="segmented" role="group" aria-label="Top N">
          {NS.map((n) => (
            <button key={n} type="button" aria-pressed={ns.includes(n)}
                    onClick={() => setNs((c) => (c.includes(n) ? c.filter((x) => x !== n) : [...c, n]))}>
              {n === 1 ? 'Win' : `Top ${n}`}
            </button>
          ))}
        </div>
      </div>
      <div className="metrics">
        <Metric label="Edges" value={rows.length} />
        <Metric label="Exposure" value={usd(exposure)} />
        <Metric label="Expected return" value={usd(expected)} />
        <Metric label="Portfolio ROI" value={exposure > 0 ? ((exposure + expected) / exposure).toFixed(3) : '—'} />
      </div>
      {q.data?.payload && rows.length === 0 && <Empty>No edges match these filters.</Empty>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Player</th><th>Top</th><th>Side</th><th className="num">Ask</th>
                {COLUMNS.map(([key, label]) => (
                  <th key={key} className="num" aria-sort={sort === key ? 'descending' : 'none'}>
                    <button className="sort" onClick={() => setSort(key)}>{label}{sort === key ? ' ↓' : ''}</button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.ticker}-${r.side}-${r.ask}-${i}`}>
                  <td>{r.player}</td><td>{r.n}</td><td>{r.side}</td>
                  <td className="num">{Math.round(r.ask * 100)}¢</td>
                  <td className="num">{usd(r.expected_return)}</td><td className="num">{r.roi.toFixed(3)}</td>
                  <td className="num">{r.ev.toFixed(3)}</td><td className="num">{r.qty}</td><td className="num">{usd(r.total_cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {unmatched.length > 0 && <details><summary>{unmatched.length} unmatched player name(s)</summary><ul className="plain">{unmatched.map((u) => <li key={u}>{u}</li>)}</ul></details>}
    </section>
  );
}
