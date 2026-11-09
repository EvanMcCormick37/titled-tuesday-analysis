import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { req } from '../api';
import { useApp } from '../App';
import { ErrorNote } from '../components';

type Adjustments = {
  cut_players: string[];
  keep_players: string[];
  p_participate_overrides: Record<string, number>;
  p_nudges: Record<string, number>;
  global_nudge: number;
};
type Data = { event_date: string; adjustments: Adjustments; players: string[] };

function NameList({ title, hint, names, onChange }: { title: string; hint: string; names: string[]; onChange: (n: string[]) => void }) {
  const [text, setText] = useState('');
  const add = () => {
    const name = text.trim();
    if (name && !names.includes(name)) onChange([...names, name]);
    setText('');
  };
  return (
    <fieldset className="stack">
      <legend>{title}</legend>
      <small className="muted">{hint}</small>
      <div className="row">
        <input list="players" placeholder="Player name" aria-label={`Add to ${title}`} value={text}
               onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
        <button type="button" className="btn" onClick={add}>Add</button>
      </div>
      <div className="chips">
        {names.map((n) => (
          <span key={n} className="chip">{n}<button type="button" aria-label={`Remove ${n}`} onClick={() => onChange(names.filter((x) => x !== n))}>×</button></span>
        ))}
      </div>
    </fieldset>
  );
}

function ValueList({ title, hint, values, step, min, max, onChange }: {
  title: string; hint: string; values: Record<string, number>; step: number; min?: number; max?: number;
  onChange: (v: Record<string, number>) => void;
}) {
  const [name, setName] = useState('');
  const add = () => {
    const n = name.trim();
    if (n && !(n in values)) onChange({ ...values, [n]: min === undefined ? 0 : 0.5 });
    setName('');
  };
  return (
    <fieldset className="stack">
      <legend>{title}</legend>
      <small className="muted">{hint}</small>
      <div className="row">
        <input list="players" placeholder="Player name" aria-label={`Add to ${title}`} value={name}
               onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
        <button type="button" className="btn" onClick={add}>Add</button>
      </div>
      {Object.entries(values).map(([k, v]) => (
        <div key={k} className="row value-row">
          <span>{k}</span>
          <input type="number" aria-label={`${title}: ${k}`} step={step} min={min} max={max} value={v}
                 onChange={(e) => onChange({ ...values, [k]: Number(e.target.value) })} />
          <button type="button" className="btn small" aria-label={`Remove ${k}`}
                  onClick={() => { const { [k]: _, ...rest } = values; onChange(rest); }}>Remove</button>
        </div>
      ))}
    </fieldset>
  );
}

export default function Attendance() {
  const { market, openJob, toast } = useApp();
  const queryClient = useQueryClient();
  const q = useQuery({
    queryKey: ['adjustments', market.slug],
    queryFn: () => req<Data>(`/markets/${market.slug}/adjustments`),
    refetchOnWindowFocus: false,
  });
  const [form, setForm] = useState<Adjustments | null>(null);
  useEffect(() => { if (q.data && !form) setForm(q.data.adjustments); }, [q.data, form]);

  const save = useMutation({
    mutationFn: (recompute: boolean) => req<{ saved: number; job_id: number | null }>(
      `/markets/${market.slug}/adjustments`, 'PUT', { ...form, recompute }),
    onSuccess: (r) => {
      toast(`Saved ${r.saved} adjustment(s)`);
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      if (r.job_id !== null) openJob(r.job_id);
    },
  });

  if (q.error) return <ErrorNote error={q.error} />;
  if (!form || !q.data) return <p className="muted">Loading…</p>;
  const set = (patch: Partial<Adjustments>) => setForm({ ...form, ...patch });

  return (
    <section className="card stack">
      <h2>Attendance · {q.data.event_date}</h2>
      <p className="muted">Saving replaces all adjustments for this event. What you see here is the full set.</p>
      <datalist id="players">{q.data.players.map((p) => <option key={p} value={p} />)}</datalist>
      <div className="grid-two">
        <NameList title="Cut" hint="Will not play (chance of playing set to 0)" names={form.cut_players} onChange={(cut_players) => set({ cut_players })} />
        <NameList title="Keep" hint="Will play (chance of playing set to 1)" names={form.keep_players} onChange={(keep_players) => set({ keep_players })} />
        <ValueList title="Overrides" hint="Exact chance of playing, 0 to 1" values={form.p_participate_overrides} step={0.05} min={0} max={1} onChange={(p_participate_overrides) => set({ p_participate_overrides })} />
        <ValueList title="Nudges" hint="Log-odds shift for one player; positive raises" values={form.p_nudges} step={0.1} onChange={(p_nudges) => set({ p_nudges })} />
      </div>
      <label className="field compact">
        <span>Global nudge (log-odds, every player)</span>
        <input type="number" step="0.1" value={form.global_nudge} onChange={(e) => set({ global_nudge: Number(e.target.value) })} />
      </label>
      {save.error && <ErrorNote error={save.error} />}
      <div className="row end">
        <button className="btn" disabled={save.isPending} onClick={() => save.mutate(false)}>Save</button>
        <button className="btn primary" disabled={save.isPending} onClick={() => save.mutate(true)}>Save and recompute</button>
      </div>
    </section>
  );
}
