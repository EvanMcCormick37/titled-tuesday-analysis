import { useMutation, useQuery } from '@tanstack/react-query';
import { req, type Snapshot } from '../api';
import { useApp } from '../App';
import { DataAge, Empty, ErrorNote, Metric } from '../components';
import { usd } from '../format';

type Position = {
  ticker: string; marketTitle: string; n: number; position: string; volume: number;
  cost: number; fees: number; cost_incl_fees: number; avg_price: number; avg_price_incl_fees: number;
};
type Order = { ticker: string; side: string; price_c: number | null; qty: number | string | null; expires: string | null };
type Payload = { rows: Position[]; orders: Order[] };

export default function Portfolio() {
  const { market } = useApp();
  const path = `/markets/${market.slug}/snapshots/positions`;
  const q = useQuery({ queryKey: ['positions', market.slug], queryFn: () => req<Snapshot<Payload>>(path), refetchInterval: 5000 });
  const refresh = useMutation({ mutationFn: () => req<Snapshot<Payload>>(`${path}?force=true`) });
  const rows = q.data?.payload?.rows ?? [];
  const orders = q.data?.payload?.orders ?? [];
  const cost = rows.reduce((s, r) => s + Math.abs(r.cost), 0);
  const fees = rows.reduce((s, r) => s + r.fees, 0);

  return (
    <section className="card stack">
      <div className="row between wrap">
        <h2>Portfolio</h2>
        <DataAge fetchedAt={q.data?.fetched_at} error={q.data?.error} onRefresh={() => refresh.mutate()} busy={refresh.isPending} />
      </div>
      {q.error && <ErrorNote error={q.error} />}
      <div className="metrics">
        <Metric label="Exposure" value={usd(cost)} />
        <Metric label="Fees paid" value={usd(fees)} />
        <Metric label="Out of pocket" value={usd(cost + fees)} />
        <Metric label="Resting orders" value={orders.length} />
      </div>
      <h3>Positions</h3>
      {q.data?.payload && rows.length === 0 && <Empty>No open positions for this event.</Empty>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>Top</th><th>Side</th><th className="num">Contracts</th><th className="num">Cost</th><th className="num">Fees</th><th className="num">Avg (with fees)</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.ticker} title={r.ticker}>
                  <td>{r.marketTitle || r.ticker}</td><td>{r.n}</td><td>{r.position}</td>
                  <td className="num">{r.volume}</td><td className="num">{usd(r.cost)}</td><td className="num">{usd(r.fees)}</td>
                  <td className="num">{(r.avg_price_incl_fees * 100).toFixed(1)}¢</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>Resting orders</h3>
      {q.data?.payload && orders.length === 0 && <Empty>No resting orders for this event.</Empty>}
      {orders.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Market</th><th>Side</th><th className="num">Price</th><th className="num">Qty</th><th>Expires</th></tr></thead>
            <tbody>
              {orders.map((o, i) => (
                <tr key={i}><td>{o.ticker}</td><td>{o.side?.toUpperCase()}</td><td className="num">{o.price_c ?? '—'}¢</td><td className="num">{o.qty ?? '—'}</td><td>{o.expires ? new Date(o.expires).toLocaleString() : '—'}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
