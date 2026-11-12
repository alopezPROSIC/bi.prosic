import type { ModuleKpis } from "../types/api";
import { ChartCard } from "./ChartCard";
import { DataTable } from "./DataTable";
import { KpiCard } from "./KpiCard";
import { EmptyState, Notes } from "./States";

/** Pinta tarjetas, gráficas y tablas de un ModuleKpis (se reutiliza en el asistente). */
export function ModuleView({ data, compact = false }: { data: ModuleKpis; compact?: boolean }) {
  if (!data.available) {
    return (
      <EmptyState title="Módulo no disponible en Odoo">
        {data.reason}
        <br />
        Revisa <code>odoo_mapping.py</code> o instala el módulo correspondiente.
      </EmptyState>
    );
  }
  const charts = compact ? data.charts.slice(0, 2) : data.charts;
  return (
    <div className="space-y-4">
      {data.cards.length > 0 && (
        <div className={`grid gap-3 ${compact ? "grid-cols-2 sm:grid-cols-3" : "grid-cols-2 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-6"}`}>
          {data.cards.map((c) => (
            <KpiCard key={c.key} kpi={c} />
          ))}
        </div>
      )}
      {charts.length > 0 && (
        <div className={`grid gap-4 ${compact ? "grid-cols-1" : "grid-cols-1 xl:grid-cols-2"}`}>
          {charts.map((c) => (
            <ChartCard key={c.key} chart={c} />
          ))}
        </div>
      )}
      {!compact &&
        data.tables.map((t) => (
          <DataTable key={t.key} table={t} />
        ))}
      {!compact && <Notes notes={data.notes} />}
    </div>
  );
}
