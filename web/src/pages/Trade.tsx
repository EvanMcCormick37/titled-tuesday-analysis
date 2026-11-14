import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { ACTIVE, OPS, req, type Job, type Plan, type SchemaProp, type Strategy } from '../api';
import { useApp } from '../App';
import { Empty, ErrorNote, StatusBadge } from '../components';
import { humanize, time, usd } from '../format';
import { PlanTable } from '../JobDrawer';

type Params = Record<string, string | number | boolean | null>;

type Draft = {
  key: number;
  strategy: string;
  params: Params;
  plan: Plan | null;
  planFor: string;
  timing: 'now' | 'later';
  runAt: string;
  loop: boolean;
  interval: number;
  fallback: number;
  cap: string;
  rehearsal: boolean;
};

function defaults(s: Strategy): Params {
  const out: Params = {};
  for (const [k, p] of Object.entries(s.schema.properties ?? {})) out[k] = (p.default ?? null) as Params[string];
  return out;
}

function ParamField({ name, prop, value, onChange }: {
  name: string; prop: SchemaProp; value: Params[string]; onChange: (v: Params[string]) => void;
}) {
  const label = prop.title ?? humanize(name);
  const options = prop.anyOf?.find((a) => a.enum)?.enum;
  if (prop.type === 'boolean') {
    return (
      <label className="check" title={prop.description}>
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        {label}
      </label>
    );
  }
  if (options) {
    return (
      <label className="field">
        <span>{label}</span>
        <select value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value || null)}>
          <option value="">Both</option>
          {options.map((o) => <option key={o} value={o}>{o.toUpperCase()}</option>)}
        </select>
      </label>
    );
  }
  return (
    <label className="field">
      <span>{label}</span>
      <input type="number" inputMode="decimal" min={prop.minimum} max={prop.maximum}
             step={prop.type === 'integer' ? 1 : 'any'} value={value === null ? '' : String(value)}
             onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))} />
      {prop.description && <small>{prop.description}</small>}
    </label>
  );
}

function DraftForm({ draft, strategies, update, onQueued }: {
  draft: Draft;
  strategies: Strategy[];
  update: (patch: Partial<Draft>) => void;
  onQueued: () => void;
}) {
  const { market, toast } = useApp();
  const queryClient = useQueryClient();
  const strategy = strategies.find((s) => s.name === draft.strategy)!;
  const paramsKey = JSON.stringify(draft.params);
  const previewCurrent = draft.plan !== null && draft.planFor === paramsKey;
  const isLoop = draft.loop && strategy.can_loop;
  const needsCap = strategy.spends && !draft.rehearsal;

  const preview = useMutation({
    mutationFn: () => req<Plan>(`/markets/${market.slug}/preview`, 'POST', {
      strategy: draft.strategy, params: draft.params,
    }),
    onSuccess: (plan) => update({
      plan, planFor: paramsKey,
      cap: isLoop || draft.cap !== '' ? draft.cap : plan.exposure > 0 ? String(plan.exposure) : '',
    }),
  });

  const submit = useMutation({
    mutationFn: () => req<{ id: number }>(`/markets/${market.slug}/jobs`, 'POST', {
      strategy: draft.strategy,
      params: draft.params,
      max_exposure: needsCap ? Number(draft.cap) : null,
      run_at: draft.timing === 'later' ? new Date(draft.runAt).toISOString() : null,
      dry_run: draft.rehearsal,
      loop_interval_min: isLoop ? draft.interval : null,
      loop_fallback_hours: isLoop ? draft.fallback : null,
    }),
    onSuccess: (r) => {
      toast(`Added to queue as job #${r.id}`);
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
      onQueued();
    },
  });

  // Previewing is optional: every job re-plans when it fires anyway. If predictions
  // aren't marked complete at that point, the worker holds the job for review.
  const blocked =
    (needsCap && !(Number(draft.cap) > 0)) ||
    (draft.timing === 'later' && !draft.runAt);

  const verb = draft.rehearsal ? 'Queue rehearsal' : draft.timing === 'later' ? 'Schedule live trade' : isLoop ? 'Start live loop' : 'Queue live trade';
  const capText = needsCap && Number(draft.cap) > 0 ? ` · cap ${usd(Number(draft.cap))}` : '';

  return (
    <div className="stack">
      <div className="tabs" role="tablist" aria-label="Strategy">
        {strategies.map((s) => (
          <button key={s.name} role="tab" aria-selected={s.name === draft.strategy}
                  onClick={() => update({ strategy: s.name, params: defaults(s), plan: null, planFor: '', loop: false, cap: '' })}>
            {s.label}
          </button>
        ))}
      </div>
      <p className="muted">{strategy.description}</p>

      {Object.keys(strategy.schema.properties ?? {}).length > 0 && (
        <div className="grid-fields">
          {Object.entries(strategy.schema.properties ?? {}).map(([k, p]) => (
            <ParamField key={k} name={k} prop={p} value={draft.params[k]}
                        onChange={(v) => update({ params: { ...draft.params, [k]: v } })} />
          ))}
        </div>
      )}

      <button className="btn" onClick={() => preview.mutate()} disabled={preview.isPending}>
        {preview.isPending ? 'Building preview…' : previewCurrent ? 'Refresh preview' : 'Preview orders'}
      </button>
      {preview.error && <ErrorNote error={preview.error} />}

      {draft.plan && (
        <section className={`stack preview${previewCurrent ? '' : ' stale'}`}>
          <div className="row between">
            <h3>{draft.plan.orders.length} order(s), {draft.plan.cancels.length} cancel(s)</h3>
            <strong>{usd(draft.plan.exposure)}</strong>
          </div>
          {!previewCurrent && <p className="note warn">Settings changed since this preview.</p>}
          {draft.plan.orders.length + draft.plan.cancels.length === 0
            ? <p className="muted">Nothing qualifies right now.</p>
            : <PlanTable plan={draft.plan} />}
        </section>
      )}

      <section className="stack divider">
        <div className="row wrap">
          <div className="segmented" role="group" aria-label="When">
            <button type="button" aria-pressed={draft.timing === 'now'} onClick={() => update({ timing: 'now' })}>Now</button>
            <button type="button" aria-pressed={draft.timing === 'later'} onClick={() => update({ timing: 'later' })}>Schedule</button>
          </div>
          {strategy.can_loop && (
            <label className="check">
              <input type="checkbox" checked={draft.loop} onChange={(e) => update({ loop: e.target.checked })} /> Repeat as a loop
            </label>
          )}
          <label className="check" title="Plans at fire time but sends nothing">
            <input type="checkbox" checked={draft.rehearsal} onChange={(e) => update({ rehearsal: e.target.checked })} /> Rehearsal
          </label>
        </div>

        {draft.timing === 'later' && (
          <label className="field">
            <span>Fire at (your local time)</span>
            <input type="datetime-local" value={draft.runAt} onChange={(e) => update({ runAt: e.target.value })} />
            <small>Orders are re-planned at this time. If the worker is more than 15 minutes late the job expires.</small>
          </label>
        )}

        {isLoop && (
          <div className="grid-fields">
            <label className="field"><span>Minutes between sweeps</span>
              <input type="number" min="1" value={draft.interval} onChange={(e) => update({ interval: Number(e.target.value) })} /></label>
            <label className="field"><span>Hours to wait if nothing found</span>
              <input type="number" min="0.1" step="0.1" value={draft.fallback} onChange={(e) => update({ fallback: Number(e.target.value) })} /></label>
          </div>
        )}

        {needsCap && (
          <label className="field">
            <span>{isLoop ? 'Total budget across all sweeps ($)' : 'Max exposure ($)'}</span>
            <input type="number" min="0" step="1" inputMode="decimal" value={draft.cap}
                   onChange={(e) => update({ cap: e.target.value })} />
            <small>{isLoop ? 'The loop ends when a sweep would exceed what is left.' : 'If the fresh plan costs more than this when it fires, the job holds for your review.'}</small>
          </label>
        )}

        {submit.error && <ErrorNote error={submit.error} />}
        <button className="btn primary" disabled={blocked || submit.isPending} onClick={() => submit.mutate()}>
          {submit.isPending ? 'Adding…' : `${verb}${capText}`}
        </button>
      </section>
    </div>
  );
}

function Queue({ jobs, labelFor }: { jobs: Job[]; labelFor: (n: string) => string }) {
  const { openJob } = useApp();
  const active = jobs.filter((j) => ACTIVE.includes(j.status));
  const finished = jobs.filter((j) => !ACTIVE.includes(j.status)).slice(0, 25);

  const row = (j: Job) => (
    <li key={j.id}>
      <button className="job" onClick={() => openJob(j.id)}>
        <StatusBadge job={j} />
        <span className="job-main">
          <strong>{OPS[j.strategy] ?? labelFor(j.strategy)}{j.dry_run ? ' (rehearsal)' : ''}</strong>
          <small className="muted">
            #{j.id} · {j.status === 'queued' ? `runs ${time(j.run_at)}` : time(j.ended_at ?? j.started_at ?? j.run_at)}
            {j.loop_interval_min !== null ? ` · ${j.iterations} sweep(s), ${usd(j.spent)} spent` : ''}
          </small>
          {j.status === 'needs_review' && <small className="warn-text">{j.review_reason}</small>}
        </span>
        {j.status === 'running' && j.progress_total > 0
          ? <span className="num">{j.progress_done}/{j.progress_total}</span>
          : <span className="num muted">{j.plan_exposure != null ? usd(j.plan_exposure) : ''}</span>}
      </button>
    </li>
  );

  return (
    <div className="stack">
      <h2>Queue</h2>
      {active.length ? <ul className="jobs">{active.map(row)}</ul> : <Empty>Nothing queued.</Empty>}
      <h3>Recent</h3>
      {finished.length ? <ul className="jobs">{finished.map(row)}</ul> : <Empty>No finished jobs yet.</Empty>}
    </div>
  );
}

export default function Trade() {
  const { market, status } = useApp();
  const strategies = market.strategies;
  const nextKey = useRef(1);
  const makeDraft = (): Draft => ({
    key: nextKey.current++, strategy: strategies[0].name, params: defaults(strategies[0]),
    plan: null, planFor: '', timing: 'now', runAt: '', loop: false, interval: 15, fallback: 1,
    cap: '', rehearsal: false,
  });
  const [drafts, setDrafts] = useState<Draft[]>(() => [makeDraft()]);
  const [currentKey, setCurrentKey] = useState(drafts[0].key);
  const current = drafts.find((d) => d.key === currentKey) ?? drafts[0];

  const jobs = useQuery({
    queryKey: ['jobs', market.slug],
    queryFn: () => req<Job[]>(`/markets/${market.slug}/jobs`),
    refetchInterval: 2000,
  });

  const labelFor = (n: string) => strategies.find((s) => s.name === n)?.label ?? humanize(n);
  const addDraft = () => { const d = makeDraft(); setDrafts((all) => [...all, d]); setCurrentKey(d.key); };
  const removeDraft = (key: number) => {
    setDrafts((all) => {
      const rest = all.filter((d) => d.key !== key);
      if (rest.length === 0) { const d = makeDraft(); setCurrentKey(d.key); return [d]; }
      if (key === currentKey) setCurrentKey(rest[0].key);
      return rest;
    });
  };
  const patch = (key: number, p: Partial<Draft>) => setDrafts((all) => all.map((d) => (d.key === key ? { ...d, ...p } : d)));

  return (
    <div className="trade-layout">
      <section className="card">
        <div className="row between wrap">
          <h2>New trade{status ? ` · ${status.event_date}` : ''}</h2>
          <div className="drafts">
            {drafts.length > 1 && drafts.map((d, i) => (
              <span key={d.key} className="draft-chip" data-active={d.key === current.key}>
                <button onClick={() => setCurrentKey(d.key)}>{i + 1}. {labelFor(d.strategy)}</button>
                <button aria-label={`Discard draft ${i + 1}`} onClick={() => removeDraft(d.key)}>×</button>
              </span>
            ))}
            <button className="btn small" onClick={addDraft}>+ Draft</button>
          </div>
        </div>
        <DraftForm key={current.key} draft={current} strategies={strategies}
                   update={(p) => patch(current.key, p)} onQueued={() => removeDraft(current.key)} />
      </section>
      <section className="card">
        {jobs.error ? <ErrorNote error={jobs.error} /> : <Queue jobs={jobs.data ?? []} labelFor={labelFor} />}
      </section>
    </div>
  );
}
