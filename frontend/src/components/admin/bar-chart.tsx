"use client";

import { useEffect, useRef, useState } from "react";
import { number } from "./shared";

type Point = { label: string; value: number };

/**
 * One series, one hue (no legend; the panel title names it): thin bars with a
 * 2px gap, 4px rounded tops anchored on the baseline, a per-bar hover tooltip
 * on a hit target the full height of the plot, and a table view.
 */
export function BarChart({ data, label }: { data: Point[]; label: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [active, setActive] = useState<number | null>(null);
  useEffect(() => {
    if (!box.current) return;
    const observer = new ResizeObserver(entries => setWidth(Math.max(240, entries[0].contentRect.width)));
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  const height = 160, top = 16, bottom = 22, plot = height - top - bottom;
  const max = Math.max(1, ...data.map(d => d.value));
  const slot = width / data.length, barW = Math.max(2, slot - 2);
  const ticks = [0, Math.floor(data.length / 2), data.length - 1];
  const point = active === null ? null : data[active];
  return (
    <div>
      <div ref={box} className="relative">
        <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} className="block overflow-visible">
          <line x1={0} x2={width} y1={top + plot + 0.5} y2={top + plot + 0.5} className="stroke-border" />
          <text x={0} y={10} className="fill-muted-foreground text-[11px]">{number(max)} max</text>
          {data.map((d, i) => {
            const x = i * slot + 1, h = d.value ? Math.max(2, (d.value / max) * plot) : 0, y = top + plot - h, r = Math.min(4, barW / 2, h);
            return (
              <g key={d.label} onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)}>
                <rect x={i * slot} y={top} width={slot} height={plot} fill="transparent" />
                {h > 0 && <path d={`M${x},${top + plot} V${y + r} Q${x},${y} ${x + r},${y} H${x + barW - r} Q${x + barW},${y} ${x + barW},${y + r} V${top + plot} Z`}
                  className={active === i ? "fill-foreground" : "fill-bar"} />}
              </g>
            );
          })}
          {ticks.map((i, n) => (
            <text key={n} x={n === 0 ? 0 : n === 2 ? width : (i + 0.5) * slot} y={height - 4} textAnchor={n === 0 ? "start" : n === 2 ? "end" : "middle"} className="fill-muted-foreground text-[11px]">{data[i]?.label}</text>
          ))}
        </svg>
        {point && active !== null && (
          <div className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-md border border-border/50 bg-popover px-2.5 py-1.5 text-xs shadow-md"
            style={{ left: (active + 0.5) * slot, top: Math.max(0, top + plot - (point.value / max) * plot - 6) }}>
            <span className="text-muted-foreground">{point.label}</span> <span className="font-medium tabular-nums">{number(point.value)}</span>
          </div>
        )}
      </div>
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-muted-foreground">View as table</summary>
        <table className="mt-2 w-full text-left [&_td]:py-1 [&_th]:py-1 [&_th]:font-medium [&_th]:text-muted-foreground">
          <thead><tr><th>Day (UTC)</th><th className="text-right">{label}</th></tr></thead>
          <tbody>{data.map(d => <tr key={d.label}><td>{d.label}</td><td className="text-right tabular-nums">{number(d.value)}</td></tr>)}</tbody>
        </table>
      </details>
    </div>
  );
}
