import { useEffect, useRef, useState } from 'react';
import { pct, usd } from './format';

export type Bin = { lo: number; hi: number; count: number };

const HEIGHT = 220;
const M = { top: 22, right: 12, bottom: 30, left: 44 };

function niceTicks(lo: number, hi: number, target = 5): number[] {
  const span = hi - lo || 1;
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.abs(v) < 1e-9 ? 0 : v);
  return out;
}

const money = (v: number) => {
  const a = Math.abs(v);
  const s = a >= 1000 ? `$${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}k` : `$${a.toFixed(0)}`;
  return v < 0 ? `−${s}` : s;
};

/** Bar with rounded top corners, anchored to the baseline. */
function barPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(4, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** Distribution of simulated portfolio P&L: one bar per bin, losses and gains split at break-even. */
export default function PnlHistogram({ bins, total, ev }: { bins: Bin[]; total: number; ev: number }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(260, e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  if (!bins.length || total <= 0) return null;
  const lo = Math.min(bins[0].lo, 0, ev);
  const hi = Math.max(bins[bins.length - 1].hi, 0, ev);
  const maxShare = Math.max(...bins.map((b) => b.count)) / total;
  const yTicks = niceTicks(0, maxShare, 4);
  const yMax = Math.max(maxShare, yTicks[yTicks.length - 1] ?? maxShare);

  const iw = width - M.left - M.right;
  const ih = HEIGHT - M.top - M.bottom;
  const x = (v: number) => M.left + ((v - lo) / (hi - lo || 1)) * iw;
  const y = (share: number) => M.top + ih - (share / yMax) * ih;
  const hovered = hover === null ? null : bins[hover];

  const xTicks = niceTicks(lo, hi, Math.max(3, Math.floor(iw / 90)));
  const evLabelRight = x(ev) > M.left + iw * 0.8;

  return (
    <div ref={wrap} className="pnl-chart">
      <svg width={width} height={HEIGHT} role="img"
        aria-label={`Simulated P&L distribution, expected ${usd(ev)}`}
        onMouseLeave={() => setHover(null)}>
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={M.left + iw} y1={y(t)} y2={y(t)} className="grid" />
            <text x={M.left - 6} y={y(t)} dy="0.32em" textAnchor="end" className="axis">{pct(t, t < 0.01 && t > 0 ? 1 : 0)}</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} x={x(t)} y={HEIGHT - 10} textAnchor="middle" className="axis">{money(t)}</text>
        ))}
        {bins.map((b, i) => {
          if (!b.count) return null;
          const x0 = x(b.lo) + 1;
          const w = Math.max(1, x(b.hi) - x(b.lo) - 2);
          const top = y(b.count / total);
          const gain = (b.lo + b.hi) / 2 >= 0;
          return (
            <path key={i} d={barPath(x0, top, w, M.top + ih - top)}
              className={`bar ${gain ? 'gain' : 'loss'}${hover !== null && hover !== i ? ' dim' : ''}`} />
          );
        })}
        <line x1={M.left} x2={M.left + iw} y1={M.top + ih} y2={M.top + ih} className="baseline" />
        <line x1={x(0)} x2={x(0)} y1={M.top} y2={M.top + ih} className="ref zero" />
        <line x1={x(ev)} x2={x(ev)} y1={M.top - 4} y2={M.top + ih} className="ref ev" />
        <text x={x(ev) + (evLabelRight ? -4 : 4)} y={M.top - 8} textAnchor={evLabelRight ? 'end' : 'start'} className="ev-label">
          EV {usd(ev)}
        </text>
        {bins.map((b, i) => (
          <rect key={i} x={x(b.lo)} y={M.top} width={Math.max(1, x(b.hi) - x(b.lo))} height={ih}
            fill="transparent" onMouseEnter={() => setHover(i)} onClick={() => setHover(i)} />
        ))}
      </svg>
      {hovered && (
        <div className="chart-tip" style={{
          left: Math.min(Math.max(x((hovered.lo + hovered.hi) / 2), 90), width - 90),
          top: Math.max(0, y(hovered.count / total) - 46),
        }}>
          <strong>{money(hovered.lo)} to {money(hovered.hi)}</strong>
          <span>{pct(hovered.count / total)} of simulations</span>
        </div>
      )}
      <div className="row wrap chart-legend">
        <span><i className="swatch loss" />Loss</span>
        <span><i className="swatch gain" />Profit</span>
        <span><i className="swatch line" />Expected P&L</span>
        <span><i className="swatch dash" />Break-even</span>
      </div>
    </div>
  );
}
