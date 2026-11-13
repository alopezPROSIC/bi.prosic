import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useFilters } from "../hooks/useFilters";
import { formatCell, formatDate } from "../lib/format";
import type { FleetQuery } from "../types/api";
import { IconDownload } from "./Icons";

const NUMERIC = new Set(["money", "number", "percent", "days"]);
const PAGE_SIZE = 50;
const int = new Intl.NumberFormat("es-MX");

type Extra = Omit<FleetQuery, "formato" | "desde" | "hasta" | "warehouse_id">;
const EMPTY: Extra = { estado: null, familia: "", subfamilia: "", centro: "", propietario: "", subasta: "", q: "" };

function Select({ label, value, onChange, options }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-xs text-ink-2">
      {label}
      <select className="input w-full" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Todos</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

const asOptions = (xs: string[] | undefined) => (xs ?? []).map((x) => ({ value: x, label: x }));

/**
 * Registros del histórico de flota guardados en el sistema: un renglón por equipo y día («Detalle diario») o por
 * equipo al cierre de cada mes («Corte mensual»). Se consultan por página y se descargan completos con los filtros.
 */
export function FleetRecords() {
  const { filters } = useFilters();
  const [formato, setFormato] = useState<FleetQuery["formato"]>("diario");
  const [range, setRange] = useState({ desde: filters.date_from, hasta: filters.date_to });
  const [extra, setExtra] = useState<Extra>(EMPTY);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // El periodo de la página se usa como valor inicial; aquí se puede ampliar a todo el histórico.
  useEffect(() => setRange({ desde: filters.date_from, hasta: filters.date_to }), [filters.date_from, filters.date_to]);
  useEffect(() => {
    const t = window.setTimeout(() => setExtra((e) => ({ ...e, q: search.trim() })), 350);
    return () => window.clearTimeout(t);
  }, [search]);

  const query: FleetQuery = { formato, ...range, warehouse_id: filters.warehouse_id, ...extra };
  const key = JSON.stringify(query);
  useEffect(() => setPage(1), [key]);

  const { data, isFetching, isError, error: qError } = useQuery({
    queryKey: ["fleet-records", key, page],
    queryFn: () => api.fleetRecords(query, page, PAGE_SIZE),
    placeholderData: keepPreviousData,
  });

  const set = (patch: Partial<Extra>) => setExtra((e) => ({ ...e, ...patch }));
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  const tooMany = !!data && data.total > data.max_export_rows;
  const filtered = Object.entries(extra).some(([, v]) => v !== null && v !== "") || search !== "";

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.downloadFleetRecords(query);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card min-w-0 p-4">
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">Registros del histórico</h3>
          <p className="text-xs text-muted">
            Guardados en el sistema desde {data ? formatDate(data.first_day) : "…"} (primer registro en Odoo) hasta{" "}
            {data ? formatDate(data.last_day) : "…"}.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 print:hidden">
          <div className="inline-flex rounded-lg border border-line p-0.5 text-xs" role="group" aria-label="Estructura">
            {(["diario", "corte"] as const).map((f) => (
              <button
                key={f}
                className={`rounded-md px-2.5 py-1 ${formato === f ? "bg-line font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
                aria-pressed={formato === f}
                onClick={() => setFormato(f)}
              >
                {f === "diario" ? "Detalle diario" : "Corte mensual"}
              </button>
            ))}
          </div>
          <button
            className="btn-ghost text-xs"
            onClick={download}
            disabled={busy || !data || data.total === 0 || tooMany}
            title={tooMany ? `Excel abre hasta ${int.format(data!.max_export_rows)} renglones: acorta el periodo, filtra o usa «Corte mensual»` : "Descargar todos los renglones filtrados (CSV para Excel, fechas dd/mm/aaaa)"}
          >
            <IconDownload width={14} height={14} /> {busy ? "Preparando…" : `Descargar${data ? ` ${int.format(data.total)}` : ""}`}
          </button>
        </div>
      </header>

      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5 print:hidden">
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          Desde
          <input type="date" className="input w-full" value={range.desde} min={data?.first_day} max={range.hasta}
            onChange={(e) => e.target.value && setRange((r) => ({ ...r, desde: e.target.value }))} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          Hasta
          <input type="date" className="input w-full" value={range.hasta} min={range.desde} max={data?.last_day}
            onChange={(e) => e.target.value && setRange((r) => ({ ...r, hasta: e.target.value }))} />
        </label>
        <Select label="Estado" value={extra.estado ? String(extra.estado) : ""} onChange={(v) => set({ estado: v ? Number(v) : null })}
          options={(data?.options.estados ?? []).map((s) => ({ value: String(s.id), label: s.label }))} />
        <Select label="Familia" value={extra.familia ?? ""} onChange={(v) => set({ familia: v })} options={asOptions(data?.options.familia)} />
        <Select label="SubFamilia" value={extra.subfamilia ?? ""} onChange={(v) => set({ subfamilia: v })} options={asOptions(data?.options.subfamilia)} />
        <Select label="Centro de costo" value={extra.centro ?? ""} onChange={(v) => set({ centro: v })} options={asOptions(data?.options.cost_center)} />
        <Select label="Propietario" value={extra.propietario ?? ""} onChange={(v) => set({ propietario: v })} options={asOptions(data?.options.owner)} />
        <Select label="Subasta" value={extra.subasta ?? ""} onChange={(v) => set({ subasta: v as Extra["subasta"] })}
          options={[{ value: "si", label: "Sí" }, { value: "no", label: "No" }]} />
        <label className="col-span-2 flex flex-col gap-1 text-xs text-ink-2">
          Buscar equipo
          <input type="search" className="input w-full" placeholder="Serie, nombre o producto" value={search}
            onChange={(e) => setSearch(e.target.value)} maxLength={80} />
        </label>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs print:hidden">
        <button className="btn-ghost text-xs" disabled={!data || range.desde === data.first_day}
          onClick={() => data && setRange({ desde: data.first_day, hasta: data.last_day })}>
          Todo el histórico
        </button>
        {filtered && (
          <button className="btn-ghost text-xs" onClick={() => { setExtra(EMPTY); setSearch(""); }}>
            Quitar filtros
          </button>
        )}
        {formato === "corte" && <span className="text-muted">Un renglón por equipo al cierre de cada mes (el último corte es la fecha «Hasta»).</span>}
        {error && <span className="text-bad">{error}</span>}
      </div>

      {isError ? (
        <p className="py-6 text-center text-sm text-bad">{(qError as Error).message}</p>
      ) : !data ? (
        <p className="py-10 text-center text-sm text-muted">Cargando registros…</p>
      ) : data.rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Sin registros para los filtros seleccionados.</p>
      ) : (
        <div className={`-mx-4 overflow-x-auto px-4 ${isFetching ? "opacity-60 transition-opacity" : ""}`}>
          <table className="tabular w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                {data.columns.map((c) => (
                  <th key={c.key} className={`whitespace-nowrap px-2 py-2 font-medium ${NUMERIC.has(c.type) ? "text-right" : ""}`}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={i} className="border-b border-line/60 last:border-0 hover:bg-line/30">
                  {data.columns.map((c) => (
                    <td key={c.key} className={`whitespace-nowrap px-2 py-1.5 ${NUMERIC.has(c.type) ? "text-right" : "max-w-[18rem] truncate"}`}>
                      {formatCell(r[c.key], c.type)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data && data.total > 0 && (
        <footer className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-ink-2 print:hidden">
          <span>
            {int.format((page - 1) * PAGE_SIZE + 1)}–{int.format(Math.min(page * PAGE_SIZE, data.total))} de {int.format(data.total)} renglones ·{" "}
            {formatDate(data.desde)} – {formatDate(data.hasta)}
          </span>
          <span className="flex items-center gap-1">
            <button className="btn-ghost px-2 py-1 text-xs" disabled={page <= 1} onClick={() => setPage(1)} aria-label="Primera página">«</button>
            <button className="btn-ghost px-2 py-1 text-xs" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Anterior</button>
            <span className="px-1">Página {int.format(page)} de {int.format(pages)}</span>
            <button className="btn-ghost px-2 py-1 text-xs" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Siguiente</button>
            <button className="btn-ghost px-2 py-1 text-xs" disabled={page >= pages} onClick={() => setPage(pages)} aria-label="Última página">»</button>
          </span>
        </footer>
      )}
    </section>
  );
}
