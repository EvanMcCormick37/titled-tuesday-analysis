import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useContext, useEffect, useState, type FormEvent } from 'react';
import { ApiError, req, type Market, type Status } from './api';
import { Banner, ConfirmDialog, ErrorNote, Toasts, useToasts } from './components';
import { JobDrawer } from './JobDrawer';
import Attendance from './pages/Attendance';
import Edges from './pages/Edges';
import Logs from './pages/Logs';
import Odds from './pages/Odds';
import Pipeline from './pages/Pipeline';
import Portfolio from './pages/Portfolio';
import Trade from './pages/Trade';

type Ctx = {
  market: Market;
  status: Status | undefined;
  openJob: (id: number) => void;
  toast: (message: string, kind?: 'ok' | 'error') => void;
};
const AppContext = createContext<Ctx | null>(null);
export const useApp = () => useContext(AppContext)!;

const PAGES = [
  ['trade', 'Trade', Trade],
  ['edges', 'Edges', Edges],
  ['portfolio', 'Portfolio', Portfolio],
  ['odds', 'Odds', Odds],
  ['attendance', 'Attendance', Attendance],
  ['pipeline', 'Pipeline', Pipeline],
  ['logs', 'Logs', Logs],
] as const;

type Route = { slug: string; page: string };

/** The URL is /<market>/<page>; an older /<page> link opens the first market. */
function useRoute(slugs: string[]): [Route, (next: Route) => void] {
  const read = (): Route => {
    const parts = location.pathname.split('/').filter(Boolean);
    const hasMarket = slugs.includes(parts[0]);
    return { slug: hasMarket ? parts[0] : slugs[0], page: (hasMarket ? parts[1] : parts[0]) || 'trade' };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onPop = () => setRoute(read());
    addEventListener('popstate', onPop);
    return () => removeEventListener('popstate', onPop);
  }, []);
  return [route, (next) => { history.pushState(null, '', `/${next.slug}/${next.page}`); setRoute(next); }];
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('');
  const login = useMutation({
    mutationFn: () => req('/login', 'POST', { password }),
    onSuccess: onDone,
  });
  const submit = (e: FormEvent) => { e.preventDefault(); login.mutate(); };
  return (
    <main className="login">
      <form className="card stack" onSubmit={submit}>
        <h1>Trading Desk</h1>
        <label className="field">
          <span>Password</span>
          <input type="password" autoFocus autoComplete="current-password" value={password}
                 onChange={(e) => setPassword(e.target.value)} />
        </label>
        {login.error && <ErrorNote error={login.error} />}
        <button className="btn primary" disabled={login.isPending || !password}>
          {login.isPending ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}

function Shell({ markets }: { markets: Market[] }) {
  const queryClient = useQueryClient();
  const [{ slug, page }, navigate] = useRoute(markets.map((m) => m.slug));
  const market = markets.find((m) => m.slug === slug) ?? markets[0];
  const setPage = (p: string) => navigate({ slug: market.slug, page: p });
  const [jobId, setJobId] = useState<number | null>(null);
  const [killing, setKilling] = useState(false);
  const { toasts, toast } = useToasts();

  const status = useQuery({
    queryKey: ['status', market.slug],
    queryFn: () => req<Status>(`/markets/${market.slug}/status`),
    refetchInterval: 5000,
  });

  const kill = useMutation({
    mutationFn: () => req<{ id: number }>(`/markets/${market.slug}/kill`, 'POST'),
    onSuccess: (r) => {
      setKilling(false);
      setJobId(r.id);
      toast('Cancelling all resting orders');
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
    },
    onError: (e) => { setKilling(false); toast((e as Error).message, 'error'); },
  });

  const logout = async () => {
    await req('/logout', 'POST');
    queryClient.clear();
    location.href = '/';
  };

  const Page = (PAGES.find(([key]) => key === page) ?? PAGES[0])[2];

  return (
    <AppContext.Provider value={{ market, status: status.data, openJob: setJobId, toast }}>
      <header className="topbar">
        <div className="brand">
          <strong>Trading Desk</strong>
          {markets.length > 1 ? (
            <select aria-label="Market" value={market.slug} onChange={(e) => navigate({ slug: e.target.value, page })}>
              {markets.map((m) => <option key={m.slug} value={m.slug}>{m.label}</option>)}
            </select>
          ) : <span className="muted">{market.label}</span>}
        </div>
        <nav aria-label="Pages">
          {PAGES.map(([key, label]) => (
            <a key={key} href={`/${market.slug}/${key}`} aria-current={page === key ? 'page' : undefined}
               onClick={(e) => { e.preventDefault(); setPage(key); }}>{label}</a>
          ))}
        </nav>
        <div className="top-actions">
          <button className="btn danger" onClick={() => setKilling(true)}>Cancel all orders</button>
          <button className="btn ghost" onClick={logout}>Sign out</button>
        </div>
      </header>
      <Banner status={status.data} onGoPipeline={() => setPage('pipeline')} />
      {/* Keyed so form values and filters never carry over to another market. */}
      <main className="page"><Page key={market.slug} /></main>
      {jobId !== null && <JobDrawer id={jobId} onClose={() => setJobId(null)} />}
      {killing && (
        <ConfirmDialog
          title="Cancel all orders?"
          body={`This cancels every resting ${market.label} order for ${status.data?.event_date ?? 'the next event'}, drops all queued trades, and stops any trade in progress. Live, immediately.`}
          confirmLabel={kill.isPending ? 'Cancelling…' : 'Yes, cancel everything'}
          busy={kill.isPending}
          onConfirm={() => kill.mutate()}
          onCancel={() => setKilling(false)}
        />
      )}
      <Toasts toasts={toasts} />
    </AppContext.Provider>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ['me'], queryFn: () => req<{ authenticated: boolean }>('/me') });
  const markets = useQuery({
    queryKey: ['markets'],
    queryFn: () => req<Market[]>('/markets'),
    enabled: me.data?.authenticated === true,
    staleTime: Infinity,
  });

  if (me.isLoading) return <p className="center muted">Loading…</p>;
  if (!me.data?.authenticated || (markets.error instanceof ApiError && markets.error.status === 401)) {
    return <Login onDone={() => queryClient.invalidateQueries({ queryKey: ['me'] })} />;
  }
  if (markets.error) return <div className="center"><ErrorNote error={markets.error} /></div>;
  if (!markets.data) return <p className="center muted">Loading…</p>;
  return <Shell markets={markets.data} />;
}
