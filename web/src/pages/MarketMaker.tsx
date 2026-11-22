import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { req, type SchemaProp } from '../api';
import { useApp } from '../App';
import { ConfirmDialog, Empty, ErrorNote, Metric, Segmented } from '../components';
import { ago, time, usd } from '../format';

type Q = { price: number; size: number } | null;
type SideRow = { fair: number; note: string; war: boolean; best_ex_own: number | null; anchor: Q; scout: Q };
type QuoteRow = {
  player: string; username: string | null; n: number; fair_yes: number | null; status: string;
  stale: boolean; position: number; yes?: SideRow; no?: SideRow;
  live: { side: string; tier: string; price: number; remaining: number; state: string }[];
};
type MMState = {
  live: boolean; scouts: boolean; event_date: string | null; t_cut: string | null; window_open: boolean;
  cutoff_done: boolean; paused: boolean; pause_reason: string | null; held: boolean;
  fv_version: string | null; fv_errors: string[]; ws_connected: boolean; stale_books: number;
  markets: number; unmatched: string[]; orders: { anchor: number; scout: number };
  resting_notional: number; tokens: number; oms: Record<string, number>;
  quotes: Record<string, QuoteRow>;
};
type Override = {
  username: string; player_name: string | null; status: 'active' | 'pulled'; yes_factor: number;
  no_factor: number; wipes: number; markup_override: number | null; updated_at: string | null; updated_by: string | null;
};
type Alert = {
  id: number; ts: string; level: string; kind: string; username: string | null; ticker: string | null;
  message: string; acknowledged: boolean;
};
type Fill = { trade_id: string; ticker: string; side: string; tier: string; price_c: number; count: number; username: string | null; ts: string };
type Overview = {
  online: boolean; heartbeat_age: number | null; mode: string | null;
  control: { paused: boolean; reason: string | null };
  params: Record<string, number | boolean>;
  param_schema: { properties: Record<string, SchemaProp> };
  state: Partial<MMState>; overrides: Override[]; alerts: Alert[]; fills: Fill[];
};

const LEVEL: Record<string, string> = { info: 'info', warning: 'warn', error: 'bad' };
const cents = (q: Q) => (q ? `${q.price}¢ × ${q.size}` : '—');

function SideCell({ s }: { s?: SideRow }) {
  if (!s) return <td className="muted">—</td>;
  return (
    <td title={s.note}>
      <div>{s.scout ? <strong>{cents(s.scout)}</strong> : <span className="muted">no scout</span>}</div>
      <small className="muted">anchor {cents(s.anchor)} · best other {s.best_ex_own ?? '—'}¢{s.war ? ' · war' : ''}</small>
    </td>
  );
}

function RepostForm({ username, onDone }: { username: string; onDone: () => void }) {
  const { market, toast } = useApp();
  const [markup, setMarkup] = useState('');
  const repost = useMutation({
    mutationFn: () => req(`/markets/${market.slug}/mm/players/${encodeURIComponent(username)}/repost`, 'POST',
      { markup: markup ? Number(markup) : null }),
    onSuccess: () => { toast('Player reposted'); onDone(); },
    onError: (e) => toast((e as Error).message, 'error'),
  });
  return (
    <span className="row">
      <input className="small" style={{ width: 80 }} type="number" step="0.05" min="1" max="10"
             placeholder="markup" aria-label="Repost markup" value={markup} onChange={(e) => setMarkup(e.target.value)} />
      <button className="btn small primary" disabled={repost.isPending} onClick={() => repost.mutate()}>Repost</button>
    </span>
  );
}

function ParamsForm({ ov, onSaved }: { ov: Overview; onSaved: () => void }) {
  const { market, toast } = useApp();
  const [draft, setDraft] = useState<Record<string, number | boolean>>(ov.params);
  useEffect(() => setDraft(ov.params), [JSON.stringify(ov.params)]);
  const save = useMutation({
    mutationFn: () => req(`/markets/${market.slug}/mm/params`, 'PUT', draft),
    onSuccess: () => { toast('Parameters saved; tt-mm picks them up within seconds'); onSaved(); },
    onError: (e) => toast((e as Error).message, 'error'),
  });
  return (
    <details className="card stack">
      <summary><strong>Parameters</strong></summary>
      <div className="grid-fields">
        {Object.entries(ov.param_schema.properties).map(([key, prop]) => (
          <label key={key} className="field" title={prop.description}>
            <span>{prop.title ?? key}</span>
            {prop.type === 'boolean' ? (
              <input type="checkbox" checked={Boolean(draft[key])}
                     onChange={(e) => setDraft({ ...draft, [key]: e.target.checked })} />
            ) : (
              <input type="number" step="any" value={String(draft[key] ?? '')}
                     onChange={(e) => setDraft({ ...draft, [key]: Number(e.target.value) })} />
            )}
            {prop.description && <small>{prop.description}</small>}
          </label>
        ))}
      </div>
      <div className="row end">
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate()}>Save parameters</button>
      </div>
    </details>
  );
}

export default function MarketMaker() {
  const { market, toast } = useApp();
  const qc = useQueryClient();
  const key = ['mm', market.slug];
  const q = useQuery({ queryKey: key, queryFn: () => req<Overview>(`/markets/${market.slug}/mm`), refetchInterval: 3000 });
  const refresh = () => qc.invalidateQueries({ queryKey: key });
  const [confirmPause, setConfirmPause] = useState(false);
  const [filter, setFilter] = useState<'quoted' | 'all'>('quoted');

  const pause = useMutation({
    mutationFn: (paused: boolean) => req(`/markets/${market.slug}/mm/pause`, 'POST', { paused }),
    onSuccess: (_r, paused) => { setConfirmPause(false); toast(paused ? 'Pausing: quotes will be cancelled' : 'Resumed'); refresh(); },
    onError: (e) => toast((e as Error).message, 'error'),
  });
  const pull = useMutation({
    mutationFn: (username: string) => req(`/markets/${market.slug}/mm/players/${encodeURIComponent(username)}/pull`, 'POST'),
    onSuccess: () => { toast('Player pulled'); refresh(); },
  });
  const ack = useMutation({ mutationFn: (id: number) => req(`/mm/alerts/${id}/ack`, 'POST'), onSuccess: refresh });
  const ackAll = useMutation({ mutationFn: () => req(`/markets/${market.slug}/mm/alerts/ack-all`, 'POST'), onSuccess: refresh });

  const ov = q.data;
  const st = ov?.state ?? {};
  const quotes = useMemo(() => {
    const rows = Object.entries(st.quotes ?? {});
    const shown = filter === 'all' ? rows : rows.filter(([, r]) => r.live.length || r.yes?.anchor || r.no?.anchor);
    return shown.sort(([, a], [, b]) => a.n - b.n || (b.fair_yes ?? 0) - (a.fair_yes ?? 0));
  }, [st.quotes, filter]);
  const unacked = ov?.alerts.filter((a) => !a.acknowledged) ?? [];
  const paused = ov?.control.paused ?? false;

  return (
    <div className="stack">
      <section className="card stack">
        <div className="row between wrap">
          <h2>Market maker</h2>
          <div className="row wrap">
            {ov && <span className={`badge ${ov.online ? 'good' : 'bad'}`}>{ov.online ? 'Online' : `Offline${ov.heartbeat_age != null ? ` (${Math.round(ov.heartbeat_age)}s)` : ''}`}</span>}
            {ov?.mode && <span className={`badge ${ov.mode === 'live' ? 'warn' : 'info'}`}>{ov.mode === 'live' ? 'LIVE' : 'Shadow (sends nothing)'}</span>}
            {paused && <span className="badge bad">Paused{ov?.control.reason ? `: ${ov.control.reason}` : ''}</span>}
            {st.held && <span className="badge bad">Held: model run failed checks</span>}
            {paused
              ? <button className="btn primary" disabled={pause.isPending} onClick={() => pause.mutate(false)}>Resume</button>
              : <button className="btn danger" onClick={() => setConfirmPause(true)}>Pause</button>}
          </div>
        </div>
        {q.error && <ErrorNote error={q.error} />}
        {ov && !ov.mode && <Empty>tt-mm has not reported yet. Is the service running?</Empty>}
        {ov?.mode && (
          <div className="metrics">
            <Metric label="Event" value={st.event_date ?? '—'} />
            <Metric label="Cutoff" value={st.cutoff_done ? 'Passed' : time(st.t_cut)} />
            <Metric label="Orders (scout / anchor)" value={`${st.orders?.scout ?? 0} / ${st.orders?.anchor ?? 0}`} />
            <Metric label="Resting" value={usd(st.resting_notional)} />
            <Metric label="Feed" value={st.ws_connected ? `${st.stale_books ?? 0} stale of ${st.markets ?? 0}` : 'Disconnected'} />
            <Metric label="Rate tokens" value={st.tokens ?? '—'} />
          </div>
        )}
        {st.fv_errors && st.fv_errors.length > 0 && <p className="muted">Fair values not loaded: {st.fv_errors.join('; ')}</p>}
        {st.unmatched && st.unmatched.length > 0 && (
          <p className="muted">Unmatched Kalshi names (not quoted): {st.unmatched.join(', ')}</p>
        )}
      </section>

      <section className="card stack">
        <div className="row between wrap">
          <h3>Alerts {unacked.length > 0 && <span className="badge warn">{unacked.length} new</span>}</h3>
          {unacked.length > 0 && (
            <button className="btn small ghost" disabled={ackAll.isPending} onClick={() => ackAll.mutate()}>Dismiss all</button>
          )}
        </div>
        {ov && ov.alerts.length === 0 && <Empty>No alerts yet.</Empty>}
        <ul className="stack" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {ov?.alerts.slice(0, 30).map((a) => (
            <li key={a.id} className="row between wrap" style={{ opacity: a.acknowledged ? 0.6 : 1 }}>
              <span className="row wrap">
                <span className={`badge ${LEVEL[a.level] ?? 'neutral'}`}>{a.kind.replace(/_/g, ' ')}</span>
                <span>{a.message}</span>
                <small className="muted">{ago(a.ts)}</small>
              </span>
              {!a.acknowledged && <button className="btn small ghost" onClick={() => ack.mutate(a.id)}>Dismiss</button>}
            </li>
          ))}
        </ul>
      </section>

      <section className="card stack">
        <h3>Player adjustments</h3>
        <p className="muted">Each anchor wipe cuts that player's fair value by the wipe ratio across all of their
          markets; reaching the wipe limit pulls them. A new model run clears the cuts but not pulls.</p>
        {ov && ov.overrides.length === 0 && <Empty>No adjustments this event.</Empty>}
        {ov && ov.overrides.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Player</th><th>Status</th><th className="num">Wipes</th><th className="num">YES ×</th><th className="num">NO ×</th><th className="num">Markup</th><th /></tr></thead>
              <tbody>
                {ov.overrides.map((o) => (
                  <tr key={o.username}>
                    <td>{o.player_name ?? o.username}</td>
                    <td><span className={`badge ${o.status === 'pulled' ? 'bad' : 'good'}`}>{o.status}</span></td>
                    <td className="num">{o.wipes}</td>
                    <td className="num">{o.yes_factor.toFixed(2)}</td>
                    <td className="num">{o.no_factor.toFixed(2)}</td>
                    <td className="num">{o.markup_override ?? '—'}</td>
                    <td><RepostForm username={o.username} onDone={refresh} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card stack">
        <div className="row between wrap">
          <h3>Quotes</h3>
          <Segmented label="Show" value={filter} options={[['quoted', 'Quoted'], ['all', 'All markets']]} onChange={setFilter} />
        </div>
        {ov?.mode && quotes.length === 0 && <Empty>No quotes right now.</Empty>}
        {quotes.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Player</th><th>Top</th><th className="num">Fair</th><th>YES bid</th><th>NO bid (YES offer)</th><th className="num">Pos</th><th /></tr></thead>
              <tbody>
                {quotes.map(([ticker, r]) => (
                  <tr key={ticker} title={ticker}>
                    <td>{r.player}{r.stale && <small className="muted"> · stale</small>}</td>
                    <td>{r.n === 1 ? 'Win' : r.n}</td>
                    <td className="num">{r.fair_yes != null ? `${(r.fair_yes * 100).toFixed(1)}¢` : '—'}</td>
                    <SideCell s={r.yes} />
                    <SideCell s={r.no} />
                    <td className="num">{r.position || ''}</td>
                    <td>{r.username && r.status !== 'pulled' &&
                      <button className="btn small ghost" onClick={() => pull.mutate(r.username!)}>Pull</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card stack">
        <h3>Fills</h3>
        {ov && ov.fills.length === 0 && <Empty>No market-maker fills this event.</Empty>}
        {ov && ov.fills.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Tier</th><th className="num">Price</th><th className="num">Qty</th></tr></thead>
              <tbody>
                {ov.fills.map((f) => (
                  <tr key={f.trade_id}>
                    <td>{time(f.ts)}</td><td>{f.ticker}</td><td>{f.side.toUpperCase()}</td><td>{f.tier}</td>
                    <td className="num">{f.price_c}¢</td><td className="num">{f.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {ov && <ParamsForm ov={ov} onSaved={refresh} />}

      {confirmPause && (
        <ConfirmDialog
          title="Pause the market maker?"
          body={`tt-mm cancels every market-maker quote on ${market.label} and stops quoting until you resume.`}
          confirmLabel="Pause" busy={pause.isPending}
          onConfirm={() => pause.mutate(true)} onCancel={() => setConfirmPause(false)}
        />
      )}
    </div>
  );
}
