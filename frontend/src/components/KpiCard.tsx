import { formatValue } from "../lib/format";
import type { KpiCard as Kpi } from "../types/api";
import { IconDown, IconUp } from "./Icons";

export function KpiCard({ kpi }: { kpi: Kpi }) {
  const d = kpi.delta_pct;
  const good = d === null ? null : d === 0 ? null : (d > 0) === kpi.higher_is_better;
  const tone = good === null ? "text-muted" : good ? "text-good" : "text-bad";
  return (
    <div className="card flex min-w-0 flex-col gap-1 p-4" title={kpi.hint ?? undefined}>
      <span className="line-clamp-2 text-xs font-medium text-ink-2" title={kpi.label}>{kpi.label}</span>
      <span className="truncate text-2xl font-semibold tracking-tight">{formatValue(kpi.value, kpi.unit, true)}</span>
      <div className="flex min-h-[1.25rem] items-center gap-1.5 text-xs">
        {d !== null ? (
          <>
            <span className={`inline-flex items-center gap-0.5 font-medium ${tone}`}>
              {d > 0 ? <IconUp /> : d < 0 ? <IconDown /> : null}
              {d > 0 ? "+" : ""}
              {d.toLocaleString("es-MX", { maximumFractionDigits: 1 })}%
            </span>
            <span className="truncate text-muted">vs {formatValue(kpi.previous, kpi.unit, true)}</span>
          </>
        ) : kpi.hint ? (
          <span className="line-clamp-2 text-muted" title={kpi.hint}>{kpi.hint}</span>
        ) : null}
      </div>
    </div>
  );
}
