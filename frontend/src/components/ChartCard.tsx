import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import { useChartColors, type ChartColors } from "../hooks/useChartColors";
import { useFilters } from "../hooks/useFilters";
import { useFilterOptions } from "../hooks/useKpis";
import { downloadCsv } from "../lib/csv";
import { formatAxis, formatValue } from "../lib/format";
import type { Chart, ChartSeries, Unit } from "../types/api";
import { DataTable } from "./DataTable";
import { IconDownload, IconTable } from "./Icons";

const PREV_KEY = "anterior";

/** Color por entidad (posición fija de la serie), nunca por rango. "anterior" va en gris neutro. */
function seriesColor(colors: ChartColors, s: ChartSeries, i: number): string {
  if (s.key === PREV_KEY) return colors.previous;
  return colors.series[i % colors.series.length];
}

function ChartTooltip({ active, payload, label, unit }: TooltipProps<number, string> & { unit: Unit }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-line bg-raised px-3 py-2 text-xs shadow-lg">
      {label !== undefined && <div className="mb-1 font-medium text-ink">{String(label)}</div>}
      {payload.map((p) => (
        <div key={String(p.dataKey)} className="flex items-center gap-2 text-ink-2">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: p.color }} />
          <span>{p.name}:</span>
          <span className="tabular font-medium text-ink">{formatValue(p.value as number, unit)}</span>
        </div>
      ))}
    </div>
  );
}

function Legend({ series, colors }: { series: ChartSeries[]; colors: ChartColors }) {
  if (series.length < 2) return null;
  return (
    <ul className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {series.map((s, i) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: seriesColor(colors, s, i), outline: s.key === PREV_KEY ? `1px dashed ${colors.muted}` : undefined }}
          />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

function truncate(s: string, n = 22) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

type DrillFn = ((row: Record<string, unknown>) => void) | undefined;

function Plot({ chart, colors, onDrill }: { chart: Chart; colors: ChartColors; onDrill?: DrillFn }) {
  const { data, series, unit, x_key: x } = chart;
  // Recharts entrega en onClick la fila original en `payload`.
  const barClick = onDrill ? (e: { payload?: Record<string, unknown> }) => e?.payload && onDrill(e.payload) : undefined;
  const barCursor = onDrill ? "pointer" : undefined;
  const axisProps = { stroke: colors.axis, tick: { fill: colors.muted, fontSize: 11 }, tickLine: false };
  const grid = <CartesianGrid stroke={colors.grid} strokeDasharray="0" vertical={false} />;
  const tooltip = <Tooltip content={<ChartTooltip unit={unit} />} cursor={{ fill: colors.grid, opacity: 0.4 }} />;

  if (chart.type === "line") {
    return (
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
          {grid}
          <XAxis dataKey={x} {...axisProps} minTickGap={16} />
          <YAxis {...axisProps} axisLine={false} width={64} tickFormatter={(v: number) => formatAxis(v, unit)} />
          <Tooltip content={<ChartTooltip unit={unit} />} cursor={{ stroke: colors.axis }} />
          {series.map((s, i) => (
            <Line
              key={s.key}
              dataKey={s.key}
              name={s.label}
              type="monotone"
              stroke={seriesColor(colors, s, i)}
              strokeWidth={2}
              strokeDasharray={s.key === PREV_KEY || s.key === "pronostico" ? "5 4" : undefined}
              dot={data.length <= 3 ? { r: 4, strokeWidth: 2, fill: colors.surface } : false}
              animationDuration={400}
              activeDot={{ r: 4, stroke: colors.surface, strokeWidth: 2 }}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  if (chart.type === "donut") {
    const total = data.reduce((a, r) => a + Number(r[series[0].key] ?? 0), 0);
    return (
      <div className="flex flex-col items-center gap-4 sm:flex-row">
        <ResponsiveContainer width="100%" height={220} className="max-w-[260px]">
          <PieChart>
            <Pie data={data} dataKey={series[0].key} nameKey={x} innerRadius="58%" outerRadius="90%" paddingAngle={1} stroke={colors.surface} strokeWidth={2} animationDuration={400}>
              {data.map((_, i) => (
                <Cell key={i} fill={colors.series[i % colors.series.length]} />
              ))}
            </Pie>
            <Tooltip content={<ChartTooltip unit={unit} />} />
          </PieChart>
        </ResponsiveContainer>
        <ul className="w-full space-y-1.5 text-sm">
          {data.map((r, i) => {
            const v = Number(r[series[0].key] ?? 0);
            return (
              <li key={i} className="flex items-center gap-2">
                <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: colors.series[i % colors.series.length] }} />
                <span className="flex-1 truncate text-ink-2">{String(r[x])}</span>
                <span className="tabular font-medium">{formatValue(v, unit)}</span>
                <span className="tabular w-12 text-right text-xs text-muted">{total ? `${Math.round((v / total) * 100)}%` : ""}</span>
              </li>
            );
          })}
        </ul>
      </div>
    );
  }

  if (chart.type === "hbar") {
    const height = Math.max(160, data.length * 30 + 30);
    return (
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }} barCategoryGap={6}>
          <CartesianGrid stroke={colors.grid} horizontal={false} />
          <XAxis type="number" {...axisProps} tickFormatter={(v: number) => formatAxis(v, unit)} />
          <YAxis type="category" dataKey={x} {...axisProps} axisLine={false} width={150} tickFormatter={(v: string) => truncate(String(v))} />
          {tooltip}
          {series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} fill={seriesColor(colors, s, i)} radius={[0, 4, 4, 0]} maxBarSize={22} animationDuration={400} onClick={barClick} cursor={barCursor} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    );
  }

  const stacked = chart.type === "stacked_bar";
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }} barGap={2}>
        {grid}
        <XAxis dataKey={x} {...axisProps} interval={0} tickFormatter={(v: string) => truncate(String(v), 12)} />
        <YAxis {...axisProps} axisLine={false} width={64} tickFormatter={(v: number) => formatAxis(v, unit)} />
        {tooltip}
        {series.map((s, i) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.label}
            stackId={stacked ? "a" : undefined}
            fill={seriesColor(colors, s, i)}
            stroke={stacked ? colors.surface : undefined}
            strokeWidth={stacked ? 1 : 0}
            radius={stacked ? (i === series.length - 1 ? [4, 4, 0, 0] : 0) : [4, 4, 0, 0]}
            maxBarSize={48}
            animationDuration={400}
            onClick={barClick}
            cursor={barCursor}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ChartCard({ chart }: { chart: Chart }) {
  const colors = useChartColors();
  const [asTable, setAsTable] = useState(false);
  const { update } = useFilters();
  const { data: opts } = useFilterOptions();
  // Drill-down: solo si el usuario puede cambiar ese filtro (no está limitado a un almacén/sucursal).
  const locked = chart.drill === "warehouse_id" ? opts?.scope?.warehouse_id : opts?.scope?.branch_id;
  const onDrill: DrillFn =
    chart.drill && !locked
      ? (row) => {
          const id = row._id;
          if (typeof id !== "number") return;
          update(chart.drill === "warehouse_id" ? { warehouseId: id } : { branchId: id });
          window.scrollTo({ top: 0, behavior: "smooth" });
        }
      : undefined;
  const empty = !chart.data.length || chart.data.every((r) => chart.series.every((s) => !Number(r[s.key])));
  const columns = [
    { key: chart.x_key, label: "Concepto", type: "text" as const, entity: null },
    ...chart.series.map((s) => ({ key: s.key, label: s.label, type: (chart.unit === "MXN" ? "money" : chart.unit === "%" ? "percent" : "number") as "money" | "percent" | "number", entity: null })),
  ];

  return (
    <section className="card min-w-0 p-4">
      <header className="mb-2 flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold">{chart.title}</h3>
        <div className="flex shrink-0 gap-1 print:hidden">
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setAsTable((v) => !v)} title={asTable ? "Ver gráfica" : "Ver tabla"} aria-pressed={asTable}>
            <IconTable width={14} height={14} />
          </button>
          <button className="btn-ghost px-2 py-1 text-xs" onClick={() => downloadCsv(chart.key, columns, chart.data)} title="Exportar a CSV">
            <IconDownload width={14} height={14} />
          </button>
        </div>
      </header>
      {empty ? (
        <p className="py-16 text-center text-sm text-muted">Sin datos para los filtros seleccionados.</p>
      ) : asTable ? (
        <DataTable table={{ key: chart.key, title: "", columns, rows: chart.data }} maxRows={30} />
      ) : (
        <>
          {chart.type !== "donut" && <Legend series={chart.series} colors={colors} />}
          <Plot chart={chart} colors={colors} onDrill={onDrill} />
        </>
      )}
    </section>
  );
}
