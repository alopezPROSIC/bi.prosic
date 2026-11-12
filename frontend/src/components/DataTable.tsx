import { useMemo, useState } from "react";
import { useFilterOptions } from "../hooks/useKpis";
import { downloadCsv } from "../lib/csv";
import { formatCell } from "../lib/format";
import type { Column, Table } from "../types/api";
import { IconDownload, IconExternal } from "./Icons";

const NUMERIC = new Set(["money", "number", "percent", "days"]);

type TableLike = Pick<Table, "key" | "title" | "columns" | "rows"> & { odoo_model?: string | null };

/** Enlace al formulario del registro en Odoo (se abre en otra pestaña; Odoo pide su propio login). */
function odooLink(base: string | null | undefined, model: string | null | undefined, id: unknown): string | null {
  if (!base || !model || typeof id !== "number") return null;
  return `${base.replace(/\/$/, "")}/web#id=${id}&model=${encodeURIComponent(model)}&view_type=form`;
}

export function DataTable({ table, maxRows = 50 }: { table: TableLike; maxRows?: number }) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [showAll, setShowAll] = useState(false);
  const { data: opts } = useFilterOptions();

  const rows = useMemo(() => {
    if (!sort) return table.rows;
    return [...table.rows].sort((a, b) => {
      const x = a[sort.key];
      const y = b[sort.key];
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * sort.dir;
    });
  }, [table.rows, sort]);

  const visible = showAll ? rows : rows.slice(0, maxRows);
  const toggleSort = (c: Column) =>
    setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: NUMERIC.has(c.type) ? -1 : 1 }));

  return (
    <section className="card min-w-0 p-4">
      <header className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{table.title}</h3>
        <button
          className="btn-ghost px-2 py-1 text-xs print:hidden"
          onClick={() => downloadCsv(table.key, table.columns, table.rows)}
          disabled={!table.rows.length}
          title="Exportar a CSV"
        >
          <IconDownload width={14} height={14} /> CSV
        </button>
      </header>
      {table.rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Sin registros para los filtros seleccionados.</p>
      ) : (
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="tabular w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                {table.columns.map((c) => (
                  <th key={c.key} className={`whitespace-nowrap px-2 py-2 font-medium ${NUMERIC.has(c.type) ? "text-right" : ""}`}>
                    <button className="hover:text-ink" onClick={() => toggleSort(c)}>
                      {c.label}
                      {sort?.key === c.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((r, i) => {
                const href = odooLink(opts?.odoo_url, table.odoo_model, r._id);
                return (
                  <tr key={i} className="border-b border-line/60 last:border-0 hover:bg-line/30">
                    {table.columns.map((c, ci) => (
                      <td key={c.key} className={`whitespace-nowrap px-2 py-1.5 ${NUMERIC.has(c.type) ? "text-right" : "max-w-[18rem] truncate"}`}>
                        {ci === 0 && href ? (
                          <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-brand hover:underline" title="Abrir en Odoo">
                            {formatCell(r[c.key], c.type)}
                            <IconExternal className="shrink-0 print:hidden" />
                          </a>
                        ) : (
                          formatCell(r[c.key], c.type)
                        )}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {rows.length > maxRows && (
        <button className="btn-ghost mt-2 text-xs print:hidden" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Mostrar menos" : `Mostrar ${rows.length - maxRows} más`}
        </button>
      )}
    </section>
  );
}
