import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { req, type ClashKind, type Lead, type LeadRound, type Policy, type ResearchEvent, type ResearchOverview } from '../api';
import { useApp } from '../App';
import { Empty, ErrorNote } from '../components';
import { ago, pct } from '../format';

const CLASH: Record<ClashKind, [string, string, string]> = {
  hard: ['Clash', 'bad', 'A round overlaps the event'],
  adjacent: ['Close', 'warn', 'A round ends or starts within a few hours of the event'],
  multi_day: ['Mid-event', 'neutral', 'Mid-tournament that day: rest day or travel'],
};

const POLICIES: [Policy, string][] = [
  ['auto', 'Let the researcher decide'],
  ['ignore', 'Ignore this event'],
  ['nudge_only', 'Nudge only'],
  ['cut', 'Cut players with a clash'],
  ['override', 'Set chance of playing'],
];

function ClashBadge({ kind }: { kind: ClashKind }) {
  const [label, tone, title] = CLASH[kind];
  return <span className={`badge ${tone}`} title={title}>{label}</span>;
}

function roundText(r: LeadRound): string {
  const when = new Date(r.starts_at).toLocaleString(undefined, { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  if (r.clash === 'hard') return `${r.name ?? 'Round'} ${when} · overlaps ${r.overlap_minutes} min`;
  const h = Math.round(((r.gap_minutes ?? 0) / 60) * 10) / 10;
  return `${r.name ?? 'Round'} ${when} · ${h} h away`;
}

function EventLink({ name, url }: { name: string; url: string | null }) {
  return url ? <a href={url} target="_blank" rel="noreferrer">{name}</a> : <span>{name}</span>;
}

function LeadRow({ lead }: { lead: Lead }) {
  const nearest = lead.rounds[0];
  return (
    <tr>
      <td>
        {lead.player_name ?? <span className="muted">Players not published</span>}
        {lead.fair_value !== null && <small className="muted"> · {pct(lead.fair_value, 0)}</small>}
      </td>
      <td><ClashBadge kind={lead.kind} /></td>
      <td>
        <EventLink name={lead.event_name} url={lead.event_url} />
        {nearest && <><br /><small className="muted">{roundText(nearest)}</small></>}
      </td>
      <td>
        {lead.match && (
          <span className={`badge ${lead.match === 'exact' || lead.match === 'strong' ? 'info' : 'neutral'}`}
                title={lead.matched_as ? `Listed as: ${lead.matched_as}` : undefined}>{lead.match}</span>
        )}
        {lead.policy !== 'auto' && <><br /><small className="muted">policy: {lead.policy.replace('_', ' ')}</small></>}
      </td>
    </tr>
  );
}

function PolicyEditor({ ev, onSaved }: { ev: ResearchEvent; onSaved: () => void }) {
  const { toast } = useApp();
  const [policy, setPolicy] = useState<Policy>(ev.policy);
  const [value, setValue] = useState<string>(ev.policy_value?.toString() ?? '');
  const [note, setNote] = useState(ev.policy_note ?? '');
  const needsValue = policy === 'nudge_only' || policy === 'override';
  const save = useMutation({
    mutationFn: () => req(`/research/events/${ev.id}/policy`, 'PUT',
      { policy, value: needsValue ? Number(value) : null, note: note || null }),
    onSuccess: () => { toast('Policy saved'); onSaved(); },
    onError: (e) => toast((e as Error).message, 'error'),
  });
  return (
    <div className="row wrap">
      <select aria-label={`Policy for ${ev.name}`} value={policy} onChange={(e) => setPolicy(e.target.value as Policy)}>
        {POLICIES.map(([p, label]) => <option key={p} value={p}>{label}</option>)}
      </select>
      {needsValue && (
        <input type="number" aria-label="Policy value" style={{ width: 90 }}
               step={policy === 'override' ? 0.05 : 0.1} min={policy === 'override' ? 0 : undefined}
               max={policy === 'override' ? 1 : undefined}
               placeholder={policy === 'override' ? '0 to 1' : 'log-odds'}
               value={value} onChange={(e) => setValue(e.target.value)} />
      )}
      <input aria-label="Why" placeholder="Why (optional)" value={note} onChange={(e) => setNote(e.target.value)}
             style={{ flex: 1, minWidth: 140 }} />
      <button className="btn small" disabled={save.isPending || (needsValue && value === '')}
              onClick={() => save.mutate()}>Save</button>
    </div>
  );
}

function EventItem({ ev, onChanged }: { ev: ResearchEvent; onChanged: () => void }) {
  const { toast } = useApp();
  const remove = useMutation({
    mutationFn: () => req(`/research/events/${ev.id}`, 'DELETE'),
    onSuccess: () => { toast('Event removed'); onChanged(); },
  });
  const dates = ev.start_date === ev.end_date ? ev.start_date : `${ev.start_date} – ${ev.end_date}`;
  return (
    <li className="research-event stack">
      <div className="row between wrap">
        <div className="row wrap">
          {ev.clash ? <ClashBadge kind={ev.clash.kind} /> : <span className="badge neutral">No clash</span>}
          <EventLink name={ev.name} url={ev.url} />
        </div>
        {ev.source === 'operator' && (
          <button className="btn small ghost" disabled={remove.isPending} onClick={() => remove.mutate()}>Remove</button>
        )}
      </div>
      <small className="muted">
        {dates}
        {ev.location && ` · ${ev.location}`}
        {ev.tier !== null && ` · tier ${ev.tier}`}
        {ev.fide_tc && ` · ${ev.fide_tc}`}
        {` · ${ev.player_count} player${ev.player_count === 1 ? '' : 's'} listed`}
        {ev.source === 'operator' ? ' · added by you' : ` · ${ev.source}`}
      </small>
      {ev.players_hint && <small className="muted">Players: {ev.players_hint}</small>}
      {ev.players && ev.players.length > 0 && <small className="muted">Players: {ev.players.join(', ')}</small>}
      {ev.clash?.rounds.slice(0, 3).map((r) => <small key={r.starts_at}>{roundText(r)}</small>)}
      <PolicyEditor ev={ev} onSaved={onChanged} />
    </li>
  );
}

function AddEvent({ eventDate, onAdded }: { eventDate: string; onAdded: () => void }) {
  const { toast } = useApp();
  const localTz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/New_York';
  const blank = { name: '', url: '', start_date: eventDate, end_date: '', round_time: '', time_zone: localTz, fide_tc: '', players: '' };
  const [f, setF] = useState(blank);
  const set = (k: keyof typeof blank) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const add = useMutation({
    mutationFn: () => req<{ id: number }>('/research/events', 'POST', {
      name: f.name.trim(), url: f.url.trim() || null, start_date: f.start_date, end_date: f.end_date || null,
      round_time: f.round_time || null, time_zone: f.time_zone, fide_tc: f.fide_tc || null,
      players: f.players.split(/[\n,;]/).map((p) => p.trim()).filter(Boolean),
    }),
    onSuccess: () => { toast('Event added'); setF(blank); onAdded(); },
    onError: (e) => toast((e as Error).message, 'error'),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); add.mutate(); };
  return (
    <form className="stack" onSubmit={submit}>
      <small className="muted">For events the researcher can't see, such as ones listed only on chess.com/events. Rounds repeat daily at the round time.</small>
      <div className="grid-two">
        <label className="field"><span>Name</span><input required value={f.name} onChange={set('name')} /></label>
        <label className="field"><span>Link</span><input type="url" value={f.url} onChange={set('url')} placeholder="https://www.chess.com/events/…" /></label>
        <label className="field"><span>First day</span><input type="date" required value={f.start_date} onChange={set('start_date')} /></label>
        <label className="field"><span>Last day</span><input type="date" value={f.end_date} onChange={set('end_date')} /></label>
        <label className="field"><span>Round time</span><input type="time" value={f.round_time} onChange={set('round_time')} /></label>
        <label className="field"><span>Time zone</span><input value={f.time_zone} onChange={set('time_zone')} placeholder="Europe/Madrid" /></label>
        <label className="field"><span>Time control</span>
          <select value={f.fide_tc} onChange={set('fide_tc')}>
            <option value="">Unknown (assume classical)</option>
            <option value="standard">Classical</option>
            <option value="rapid">Rapid</option>
            <option value="blitz">Blitz</option>
          </select>
        </label>
      </div>
      <label className="field"><span>Players</span>
        <textarea rows={2} value={f.players} onChange={set('players')} placeholder="Hikaru Nakamura, Magnus Carlsen, …" />
      </label>
      <div className="row end"><button className="btn" disabled={add.isPending || !f.name.trim()}>Add event</button></div>
    </form>
  );
}

export default function ResearchPanel() {
  const { market, openJob, toast } = useApp();
  const queryClient = useQueryClient();
  const key = ['research', market.slug];
  const q = useQuery({
    queryKey: key,
    queryFn: () => req<ResearchOverview>(`/markets/${market.slug}/research`),
    refetchInterval: (query) => (query.state.data?.run?.status === 'running' ? 5_000 : 60_000),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  const runNow = useMutation({
    mutationFn: () => req<{ id: number }>(`/markets/${market.slug}/research/run`, 'POST'),
    onSuccess: (r) => { openJob(r.id); setTimeout(refresh, 2_000); },
    onError: (e) => toast((e as Error).message, 'error'),
  });

  if (q.error) return <section className="card"><ErrorNote error={q.error} /></section>;
  if (!q.data) return <section className="card"><p className="muted">Loading research…</p></section>;
  const { run, events, event_date, window } = q.data;
  const leads = run?.for_current_event ? run.leads ?? [] : [];
  const errors = Object.entries(run?.collector_errors ?? {});
  const windowText = `${new Date(window.start).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}–${new Date(window.end).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;

  return (
    <section className="card stack">
      <div className="row between wrap">
        <h2>Research · {event_date}</h2>
        <button className="btn" disabled={runNow.isPending || run?.status === 'running'} onClick={() => runNow.mutate()}>
          {run?.status === 'running' ? 'Running…' : 'Run now'}
        </button>
      </div>
      <p className="muted">
        {run ? (
          <>
            Last run {ago(run.started_at)}
            {run.job_id !== null && <> (<button className="link" onClick={() => openJob(run.job_id!)}>log</button>)</>}
            {run.status === 'failed' && <span className="warn-text"> · failed: {run.error}</span>}
            {run.status !== 'failed' && run.status !== 'running' && <> · {run.candidate_count ?? 0} players checked · {leads.length} lead{leads.length === 1 ? '' : 's'}</>}
            {!run.for_current_event && <span className="warn-text"> · that run was for {run.tourn_date}; run again for this event</span>}
          </>
        ) : 'No research run yet.'}
        {' '}Event window {windowText} your time.
      </p>
      {errors.length > 0 && <p className="note warn-text">Source unavailable: {errors.map(([k, v]) => `${k} (${v})`).join('; ')}</p>}

      {run?.for_current_event && (leads.length === 0
        ? <Empty>No clashes found for the players who matter this week.</Empty>
        : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Player · fair value</th><th>Clash</th><th>Event</th><th>Match</th></tr></thead>
              <tbody>{leads.map((l, i) => <LeadRow key={`${l.event_id}-${l.username ?? i}`} lead={l} />)}</tbody>
            </table>
          </div>
        ))}

      <details>
        <summary>Events near {event_date} ({events.length})</summary>
        {events.length === 0 ? <Empty>No events collected yet. Run research to fetch the calendar.</Empty> : (
          <ul className="research-events">
            {events.map((ev) => <EventItem key={ev.id} ev={ev} onChanged={refresh} />)}
          </ul>
        )}
      </details>
      <details>
        <summary>Add an event</summary>
        <AddEvent eventDate={event_date} onAdded={refresh} />
      </details>
    </section>
  );
}
