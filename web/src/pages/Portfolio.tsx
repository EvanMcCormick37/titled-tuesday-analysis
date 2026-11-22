import { useMutation, useQuery } from '@tanstack/react-query';
import { req, type Snapshot } from '../api';
import { useApp } from '../App';
import { DataAge, Empty, ErrorNote, Metric } from '../components';
import { pct, usd } from '../format';
import PnlHistogram, { type Bin } from '../PnlHistogram';

type Position = {
  ticker: string; marketTitle: string; n: number; position: string; volume: number;
  cost: number; fees: number; cost_incl_fees: number; avg_price: number; avg_price_incl_fees: number;
};
type Order = { ticker: string; side: string; price_c: number | null; qty: number | string | null; expires: string | null };
type Payload = { rows: Position[]; orders: Order[] };
type SimPosition = { ticker: string; p_win: number; ev: number; expected_return: number; roi: number | null; matched: boolean };
type PnlSummary = {
  ev: number; cost: number; expected_payout: number; roi: number | null; std: number; p_profit: number;
  p05: number; p25: number; p50: number; p75: number; p95: number; min: number; max: number;
};
type PnlPayload = {
  empty?: boolean; tourn_date: string; predictions_date?: string; n_sims?: number;
  summary?: PnlSummary; histogram?: Bin[]; positions?: SimPosition[]; unmatched?: string[]; warnings?: string[];
};

const signedUsd = (v: number) => (v < 0 ? `−${usd(-v)}` : `+${usd(v)}`);

function PnlSimulation({ snap, onRefresh, busy, positionsHeld }: {
  snap: Snapshot<PnlPayload> | undefined; onRefresh: () => void; busy: boolean; positionsHeld: boolean;
}) {
  const p = snap?.payload;
  const s = p?.summary;
  return (
    <div className="stack">
      <div className="row between wrap">
        <h3>Simulated P&amp;L{p?.n_sims ? ` (${p.n_sims.toLocaleString()} tournaments)` : ''}</h3>
        <DataAge fetchedAt={snap?.fetched_at} error={snap?.error} onRefresh={onRefresh} busy={busy} />
      </div>
      {!p && !snap?.error && positionsHeld && <p className="muted">Simulating… the first run takes about a minute.</p>}
      {p?.empty && <Empty>No positions to simulate.</Empty>}
      {p?.warnings?.map((w) => <p key={w} className="note warn">{w}</p>)}
      {s && p?.histogram && (
        <>
          <div className="metrics">
            <Metric label="Expected P&L" value={signedUsd(s.ev)} />
            <Metric label="Expected payout" value={usd(s.expected_payout)} />
            <Metric label="Expected ROI" value={s.roi === null ? '—' : pct(s.roi)} />
            <Metric label="Chance of profit" value={pct(s.p_profit)} />
            <Metric label="5th–95th pct" value={<>{signedUsd(s.p05)} to {signedUsd(s.p95)}</>} />
            <Metric label="Std dev" value={usd(s.std)} />
          </div>
          <PnlHistogram bins={p.histogram} total={p.n_sims ?? 1} ev={s.ev} />
          <div className="table-wrap">
            <table>
              <thead><tr><th>Percentile</th><th className="num">Worst</th><th className="num">5th</th><th className="num">25th</th><th className="num">Median</th><th className="num">75th</th><th className="num">95th</th><th className="num">Best</th></tr></thead>
              <tbody><tr>
                <td>P&amp;L</td>
                {[s.min, s.p05, s.p25, s.p50, s.p75, s.p95, s.max].map((v, i) => <td key={i} className="num">{signedUsd(v)}</td>)}
              </tr></tbody>
            </table>
          </div>
          <small className="muted">
            Costs include fees. Uses the official predictions{p.predictions_date ? ` for ${p.predictions_date}` : ''}, with players' results drawn jointly, so positions on the same event are correlated.
          </small>
        </>
      )}
    </div>
  );
}

export default function Portfolio() {
  const { market } = useApp();
  const path = `/markets/${market.slug}/snapshots/positions`;
  const q = useQuery({ queryKey: ['positions', market.slug], queryFn: () => req<Snapshot<Payload>>(path), refetchInterval: 5000 });
  const refresh = useMutation({ mutationFn: () => req<Snapshot<Payload>>(`${path}?force=true`) });
  const pnlPath = `/markets/${market.slug}/snapshots/pnl`;
  const pnl = useQuery({ queryKey: ['pnl', market.slug], queryFn: () => req<Snapshot<PnlPayload>>(pnlPath), refetchInterval: 10000 });
  const pnlRefresh = useMutation({ mutationFn: () => req<Snapshot<PnlPayload>>(`${pnlPath}?force=true`) });
  const sim = new Map((pnl.data?.payload?.positions ?? []).map((r) => [r.ticker, r]));
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
      <PnlSimulation snap={pnl.data} onRefresh={() => pnlRefresh.mutate()} busy={pnlRefresh.isPending} positionsHeld={rows.length > 0} />
      <h3>Positions</h3>
      {q.data?.payload && rows.length === 0 && <Empty>No open positions for this event.</Empty>}
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>Top</th><th>Side</th><th className="num">Contracts</th><th className="num">Cost</th><th className="num">Fees</th><th className="num">Avg (with fees)</th><th className="num">P(win)</th><th className="num">EV</th><th className="num">Exp. P&amp;L</th></tr></thead>
            <tbody>
              {rows.map((r) => { const m = sim.get(r.ticker); return (
                <tr key={r.ticker} title={r.ticker}>
                  <td>{r.marketTitle || r.ticker}</td><td>{r.n}</td><td>{r.position}</td>
                  <td className="num">{r.volume}</td><td className="num">{usd(r.cost)}</td><td className="num">{usd(r.fees)}</td>
                  <td className="num">{(r.avg_price_incl_fees * 100).toFixed(1)}¢</td>
                  <td className="num">{m ? (m.matched ? pct(m.p_win) : 'unmatched') : '—'}</td>
                  <td className="num">{m ? usd(m.ev) : '—'}</td>
                  <td className="num">{m ? signedUsd(m.expected_return) : '—'}</td>
                </tr>
              ); })}
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
