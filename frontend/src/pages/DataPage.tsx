import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { IconCheck, IconChevronDown, IconChevronUp, IconDownload, IconPlus, IconRefresh, IconX } from "../components/Icons";
import { ErrorState } from "../components/States";
import { useAuth } from "../hooks/useAuth";
import { formatCell } from "../lib/format";
import type { ColumnType, DataField, DataOp, DataPreset, DataQuery, DataResult, DataSource, SyncRun } from "../types/api";

// ------------------------------------------------------------------ catálogos
const PRESETS: { key: DataPreset; label: string }[] = [
  { key: "mes_actual", label: "Este mes" },
  { key: "mes_anterior", label: "Mes anterior" },
  { key: "trimestre_actual", label: "Este trimestre" },
  { key: "anio_actual", label: "Este año" },
  { key: "anio_anterior", label: "Año anterior" },
  { key: "ultimos_30", label: "Últimos 30 días" },
  { key: "ultimos_90", label: "Últimos 90 días" },
  { key: "ultimos_365", label: "Últimos 12 meses" },
  { key: "personalizado", label: "Personalizado…" },
  { key: "sin_fecha", label: "Sin filtro de fecha" },
];
const OP_LABEL: Record<DataOp, string> = {
  "=": "es igual a", "!=": "es distinto de", contiene: "contiene", no_contiene: "no contiene", ">": "mayor que",
  ">=": "mayor o igual a", "<": "menor que", "<=": "menor o igual a", en: "es alguno de", vacio: "está vacío", no_vacio: "no está vacío",
};
const NUM = ["integer", "float", "monetary"];
const DATES = ["date", "datetime"];
const GROUPABLE = ["char", "integer", "date", "datetime", "boolean", "selection", "many2one"];
const GRAN: { key: string; label: string }[] = [
  { key: "day", label: "por día" }, { key: "week", label: "por semana" }, { key: "month", label: "por mes" },
  { key: "quarter", label: "por trimestre" }, { key: "year", label: "por año" },
];
const TYPE_LABEL: Record<string, string> = {
  char: "texto", text: "texto", integer: "número", float: "número", monetary: "importe", date: "fecha", datetime: "fecha y hora",
  boolean: "sí/no", selection: "lista", many2one: "relación",
};

function opsFor(type: string | undefined): DataOp[] {
  if (!type) return ["="];
  if (NUM.includes(type)) return ["=", "!=", ">", ">=", "<", "<="];
  if (DATES.includes(type)) return ["=", ">=", "<=", ">", "<", "vacio", "no_vacio"];
  if (type === "boolean") return ["="];
  if (type === "selection") return ["=", "!=", "en", "vacio", "no_vacio"];
  if (type === "many2one") return ["contiene", "no_contiene", "en", "vacio", "no_vacio"];
  return ["contiene", "no_contiene", "=", "!=", "en", "vacio", "no_vacio"];
}

function emptyQuery(src?: DataSource): DataQuery {
  return {
    nombre: "Nueva consulta", descripcion: "", fuente: src?.key ?? "", columnas: [], filtros: [],
    periodo: { campo: src?.fechas[0] ?? "", preset: "mes_actual" }, agrupar: { campos: [], sumar: [] }, orden: "", compartida: true,
  };
}

/** Lo que se manda al servidor (sin los campos de solo lectura). */
function payload(q: DataQuery): DataQuery {
  const { propietario: _p, actualizada: _a, actualizada_por: _b, editable: _e, ...rest } = q;
  return rest;
}

function fmtTs(iso: string | null | undefined) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-MX", { dateStyle: "medium", timeStyle: "short" });
}

function Section({ n, title, hint, children, actions }: { n?: string; title: string; hint?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="border-t border-line pt-4 first:border-0 first:pt-0">
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">
          {n && <span className="mr-1.5 text-muted">{n}</span>}
          {title}
        </h3>
        {actions}
      </header>
      {hint && <p className="mb-2 text-xs text-muted">{hint}</p>}
      {children}
    </section>
  );
}

// ------------------------------------------------------------------ buscador de campos
function FieldSearch({ fields, onPick, exclude = [], placeholder = "Buscar campo…" }: {
  fields: DataField[]; onPick: (f: DataField) => void; exclude?: string[]; placeholder?: string;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const t = text.trim().toLowerCase();
  const list = fields
    .filter((f) => !exclude.includes(f.campo))
    .filter((f) => !t || f.etiqueta.toLowerCase().includes(t) || f.campo.includes(t))
    .slice(0, 60);
  return (
    <div className="relative w-full sm:w-80" ref={box}>
      <input
        className="input w-full"
        placeholder={placeholder}
        value={text}
        onFocus={() => setOpen(true)}
        onChange={(e) => { setText(e.target.value); setOpen(true); }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && list[0]) { onPick(list[0]); setText(""); }
          if (e.key === "Escape") setOpen(false);
        }}
        aria-label={placeholder}
      />
      {open && (
        <ul className="absolute z-30 mt-1 max-h-72 w-full overflow-y-auto rounded-lg border border-line bg-surface p-1 shadow-lg" role="listbox">
          {list.length === 0 && <li className="px-2 py-1.5 text-xs text-muted">Sin coincidencias</li>}
          {list.map((f) => (
            <li key={f.campo}>
              <button
                type="button"
                className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-line/60"
                onClick={() => { onPick(f); setText(""); setOpen(false); }}
              >
                <span className="min-w-0 truncate">
                  {f.etiqueta}
                  <span className="ml-1.5 font-mono text-[11px] text-muted">{f.campo}</span>
                </span>
                <span className="shrink-0 text-[11px] text-muted">{f.personalizado ? "personalizado · " : ""}{TYPE_LABEL[f.tipo] ?? f.tipo}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ reglas (filtros)
function ValueInput({ field, op, value, onChange }: { field?: DataField; op: DataOp; value: DataQuery["filtros"][number]["valor"]; onChange: (v: string) => void }) {
  if (op === "vacio" || op === "no_vacio") return null;
  const v = value === null || value === undefined ? "" : String(value);
  if (field?.tipo === "boolean")
    return (
      <select className="input" value={v || "true"} onChange={(e) => onChange(e.target.value)} aria-label="Valor">
        <option value="true">Sí</option>
        <option value="false">No</option>
      </select>
    );
  if (field?.tipo === "selection" && field.opciones && op !== "en")
    return (
      <select className="input" value={v} onChange={(e) => onChange(e.target.value)} aria-label="Valor">
        <option value="">Elige…</option>
        {field.opciones.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
      </select>
    );
  if (field && DATES.includes(field.tipo))
    return <input type="date" className="input" value={v.slice(0, 10)} onChange={(e) => onChange(e.target.value)} aria-label="Valor" />;
  return (
    <input
      className="input min-w-0 flex-1"
      value={v}
      inputMode={field && NUM.includes(field.tipo) ? "decimal" : undefined}
      placeholder={op === "en" ? "Valores separados por coma (p. ej. 401-001-001, 401-001-002)" : field?.tipo === "many2one" ? "Nombre o código" : "Valor"}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Valor"
    />
  );
}

// ------------------------------------------------------------------ resultado
function ResultView({ result, onRefresh, refreshing }: { result: DataResult; onRefresh: () => void; refreshing: boolean }) {
  const NUMERIC = ["money", "number"];
  const type = (t: string): ColumnType => (t === "datetime" ? "text" : (t as ColumnType));
  const n = (x: number) => x.toLocaleString("es-MX");
  const groups = result.agrupado ? result.grupos : result.total;
  const partial = result.filas.length < groups;
  // Los totales solo se muestran si cubren todo el resultado (no los de una vista previa parcial).
  const hasTotals = Object.keys(result.totales).length > 0 && !partial;
  return (
    <section className="card min-w-0 p-4" aria-live="polite">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">
            {result.agrupado
              ? `${partial ? `Vista previa: ${n(result.filas.length)} de ` : ""}${n(result.grupos)} grupos · ${n(result.total)} registros`
              : partial
                ? `Vista previa: ${n(result.filas.length)} de ${n(result.total)} registros`
                : `${n(result.total)} registros`}
          </h3>
          <p className="text-xs text-muted">
            {result.fuente}
            {result.periodo && ` · ${formatCell(result.periodo.desde, "date")} – ${formatCell(result.periodo.hasta, "date")}`}
            {" · "}
            {result.desde_cache ? "guardado del " : "traído de Odoo el "}
            {fmtTs(result.generado)}
          </p>
        </div>
        <button className="btn-ghost border border-line px-2 py-1 text-xs" onClick={onRefresh} disabled={refreshing} title="Vuelve a consultar Odoo (como máximo una vez cada 10 minutos)">
          <IconRefresh width={14} height={14} className={refreshing ? "animate-spin" : ""} /> Actualizar desde Odoo
        </button>
      </header>
      {result.truncado && <p className="mb-2 text-xs text-warn">Hay más grupos de los que se muestran; agrega filtros para acotarlos.</p>}
      {result.filas.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted">Sin registros con estos filtros.</p>
      ) : (
        <div className="-mx-4 max-h-[32rem] overflow-auto px-4">
          <table className="tabular w-full min-w-max text-sm">
            <thead className="sticky top-0 bg-surface">
              <tr className="border-b border-line text-left text-xs text-ink-2">
                {result.columnas.map((c) => (
                  <th key={c.key} className={`whitespace-nowrap px-2 py-2 font-medium ${NUMERIC.includes(c.type) ? "text-right" : ""}`}>{c.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.filas.map((r, i) => (
                <tr key={i} className="border-b border-line/60 last:border-0 hover:bg-line/30">
                  {result.columnas.map((c) => (
                    <td key={c.key} className={`whitespace-nowrap px-2 py-1.5 ${NUMERIC.includes(c.type) ? "text-right" : "max-w-[18rem] truncate"}`}>
                      {formatCell(r[c.key], type(c.type))}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
            {hasTotals && (
              <tfoot className="sticky bottom-0 bg-surface">
                <tr className="border-t-2 border-line font-semibold">
                  {result.columnas.map((c, i) => (
                    <td key={c.key} className={`whitespace-nowrap px-2 py-2 ${NUMERIC.includes(c.type) ? "text-right" : ""}`}>
                      {c.key in result.totales ? formatCell(result.totales[c.key], type(c.type)) : i === 0 ? "Total" : ""}
                    </td>
                  ))}
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
      {partial && (
        <p className="mt-2 text-xs text-muted">La vista previa muestra los primeros {result.filas.length}. «Descargar Excel» trae todos (con totales).</p>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ editor de consulta
function QueryEditor({ initial, sources, cap, onSaved, onDeleted }: {
  initial: DataQuery; sources: DataSource[]; cap: number; onSaved: (q: DataQuery) => void; onDeleted: () => void;
}) {
  const [q, setQ] = useState<DataQuery>(initial);
  const [result, setResult] = useState<DataResult | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [needDefaults, setNeedDefaults] = useState(!initial.columnas.length && !initial.agrupar.campos.length);
  const set = (patch: Partial<DataQuery>) => { setQ((x) => ({ ...x, ...patch })); setOk(null); };
  const src = sources.find((s) => s.key === q.fuente);
  const fieldsQ = useQuery({ queryKey: ["datos-campos", q.fuente], queryFn: () => api.datos.fields(q.fuente), enabled: Boolean(q.fuente), staleTime: Infinity });
  const fields = fieldsQ.data?.campos ?? [];
  const byName = useMemo(() => new Map(fields.map((f) => [f.campo, f])), [fields]);
  const label = (c: string) => byName.get(c.split(":")[0])?.etiqueta ?? c;
  const editable = initial.editable !== false;
  const grouped = q.agrupar.campos.length > 0;

  // Al cambiar de fuente: columnas sugeridas que existan en esta instancia de Odoo y su primer campo de fecha.
  useEffect(() => {
    if (!needDefaults || !src || !fields.length) return;
    const dateField = src.fechas.find((f) => byName.has(f)) ?? "";
    setQ((x) => ({
      ...x,
      columnas: src.columnas.filter((c) => byName.has(c)).map((c) => ({ campo: c, etiqueta: "" })),
      periodo: { ...x.periodo, campo: dateField, preset: dateField ? (x.periodo.preset === "sin_fecha" ? "mes_actual" : x.periodo.preset) : "sin_fecha" },
    }));
    setNeedDefaults(false);
  }, [needDefaults, src, fields, byName]);

  const qc = useQueryClient();
  const preview = useMutation({ mutationFn: (actualizar: boolean) => api.datos.run(payload(q), { actualizar }), onSuccess: setResult });
  const excel = useMutation({ mutationFn: () => api.datos.excel(payload(q)) });
  const save = useMutation({
    mutationFn: (copy: boolean) => api.datos.save(payload(copy ? { ...q, id: null, nombre: `${q.nombre} (copia)` } : q)),
    onSuccess: (saved) => {
      qc.invalidateQueries({ queryKey: ["datos-consultas"] });
      setOk("Consulta guardada.");
      onSaved(saved);
    },
  });
  const remove = useMutation({
    mutationFn: () => api.datos.remove(q.id as string),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["datos-consultas"] }); onDeleted(); },
  });
  const error = preview.error || excel.error || save.error || remove.error;

  const dateFields = fields.filter((f) => DATES.includes(f.tipo));
  const numeric = fields.filter((f) => NUM.includes(f.tipo) && f.agrupable);
  const [ordField, ordDir] = q.orden.split(" ");
  const orderOptions = grouped
    ? [...q.agrupar.sumar.map((s) => ({ key: s, label: label(s) })), { key: "__count", label: "Registros" }]
    : q.columnas.map((c) => ({ key: c.campo, label: c.etiqueta || label(c.campo) }));

  const moveCol = (i: number, by: number) => {
    const c = [...q.columnas];
    const j = i + by;
    if (j < 0 || j >= c.length) return;
    [c[i], c[j]] = [c[j], c[i]];
    set({ columnas: c });
  };

  return (
    <div className="space-y-4">
      <div className="card space-y-5 p-4">
        <div className="grid gap-3 md:grid-cols-[1fr_1.4fr]">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-2">Nombre de la consulta</span>
            <input className="input w-full" value={q.nombre} maxLength={80} onChange={(e) => set({ nombre: e.target.value })} />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-2">Para qué sirve (opcional)</span>
            <input className="input w-full" value={q.descripcion} maxLength={300} onChange={(e) => set({ descripcion: e.target.value })} />
          </label>
        </div>

        <Section n="1" title="¿Qué datos de Odoo?">
          <select
            className="input w-full sm:w-96"
            value={q.fuente}
            onChange={(e) => {
              const s = sources.find((x) => x.key === e.target.value);
              set({ fuente: e.target.value, columnas: [], filtros: [], agrupar: { campos: [], sumar: [] }, orden: "", periodo: { campo: s?.fechas[0] ?? "", preset: "mes_actual" } });
              setNeedDefaults(true);
              setResult(null);
            }}
            aria-label="Fuente de datos"
          >
            {sources.map((s) => (
              <option key={s.key} value={s.key} disabled={!s.disponible}>{s.label}{s.disponible ? "" : ` (${s.motivo})`}</option>
            ))}
          </select>
          {src && <p className="mt-1.5 text-xs text-muted">{src.descripcion} <span className="font-mono">({src.modelo})</span></p>}
          {fieldsQ.error && <p role="alert" className="mt-1.5 text-sm text-bad">{(fieldsQ.error as Error).message}</p>}
        </Section>

        <Section n="2" title="Periodo">
          <div className="flex flex-wrap items-end gap-2">
            {q.periodo.preset !== "sin_fecha" && (
              <label className="text-sm">
                <span className="mb-1 block text-xs text-ink-2">Según la fecha de</span>
                <select className="input" value={q.periodo.campo} onChange={(e) => set({ periodo: { ...q.periodo, campo: e.target.value } })}>
                  {dateFields
                    .sort((a, b) => Number(!src?.fechas.includes(a.campo)) - Number(!src?.fechas.includes(b.campo)))
                    .map((f) => <option key={f.campo} value={f.campo}>{f.etiqueta}</option>)}
                </select>
              </label>
            )}
            <label className="text-sm">
              <span className="mb-1 block text-xs text-ink-2">Rango</span>
              <select className="input" value={q.periodo.preset} onChange={(e) => {
                const preset = e.target.value as DataPreset;
                set({ periodo: { ...q.periodo, preset, campo: q.periodo.campo || src?.fechas.find((f) => byName.has(f)) || "" } });
              }}>
                {PRESETS.filter((p) => p.key !== "sin_fecha" || src?.fecha_opcional).map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
              </select>
            </label>
            {q.periodo.preset === "personalizado" && (
              <>
                <label className="text-sm">
                  <span className="mb-1 block text-xs text-ink-2">Desde</span>
                  <input type="date" className="input" value={q.periodo.desde ?? ""} onChange={(e) => set({ periodo: { ...q.periodo, desde: e.target.value } })} />
                </label>
                <label className="text-sm">
                  <span className="mb-1 block text-xs text-ink-2">Hasta</span>
                  <input type="date" className="input" value={q.periodo.hasta ?? ""} onChange={(e) => set({ periodo: { ...q.periodo, hasta: e.target.value } })} />
                </label>
              </>
            )}
          </div>
          <p className="mt-1.5 text-xs text-muted">Los rangos relativos («Este mes») se recalculan cada vez que se abre la consulta. Máximo 3 años.</p>
        </Section>

        <Section n="3" title="Reglas: qué registros entran" hint="Todas las reglas se cumplen a la vez. En cuentas, clientes o vendedores escribe el nombre o el código: «401-001-001» encuentra la cuenta de Renta - Plataformas.">
          <div className="space-y-2">
            {q.filtros.map((f, i) => {
              const fd = byName.get(f.campo);
              const ops = opsFor(fd?.tipo);
              const upd = (patch: Partial<DataQuery["filtros"][number]>) => set({ filtros: q.filtros.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
              return (
                <div key={i} className="flex flex-wrap items-center gap-2 rounded-lg border border-line p-2">
                  <span className="min-w-[9rem] text-sm font-medium">{fd?.etiqueta ?? f.campo}</span>
                  <select className="input" value={ops.includes(f.op) ? f.op : ops[0]} onChange={(e) => upd({ op: e.target.value as DataOp })} aria-label="Condición">
                    {ops.map((o) => <option key={o} value={o}>{OP_LABEL[o]}</option>)}
                  </select>
                  <ValueInput field={fd} op={f.op} value={f.valor} onChange={(v) => upd({ valor: v })} />
                  <button className="btn-ghost ml-auto px-2 py-1 text-xs text-bad" onClick={() => set({ filtros: q.filtros.filter((_, j) => j !== i) })} aria-label="Quitar regla">
                    <IconX width={14} height={14} />
                  </button>
                </div>
              );
            })}
            {q.filtros.length < 15 && (
              <FieldSearch
                fields={fields}
                placeholder="+ Agregar regla: buscar campo…"
                onPick={(fd) => {
                  const op = opsFor(fd.tipo)[0];
                  set({ filtros: [...q.filtros, { campo: fd.campo, op, valor: fd.tipo === "boolean" ? "true" : "" }] });
                }}
              />
            )}
          </div>
        </Section>

        <Section
          n="4"
          title="Agrupar y sumar (opcional)"
          hint="Para totales en lugar de detalle: p. ej. ingresos por cuenta, cartera por cliente o unidades por almacén. Odoo hace la suma, así que es la forma más ligera de consultar periodos largos."
        >
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-ink-2">Agrupar por:</span>
              {q.agrupar.campos.map((g, i) => {
                const [name, gran] = g.split(":");
                const fd = byName.get(name);
                return (
                  <span key={g} className="inline-flex items-center gap-1 rounded-full bg-brand/10 py-0.5 pl-2.5 pr-1 text-sm text-brand">
                    {fd?.etiqueta ?? name}
                    {fd && DATES.includes(fd.tipo) && (
                      <select
                        className="rounded bg-transparent text-xs"
                        value={gran || "month"}
                        onChange={(e) => set({ agrupar: { ...q.agrupar, campos: q.agrupar.campos.map((x, j) => (j === i ? `${name}:${e.target.value}` : x)) } })}
                        aria-label="Agrupar fecha"
                      >
                        {GRAN.map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
                      </select>
                    )}
                    <button className="rounded-full p-0.5 hover:bg-brand/20" onClick={() => set({ agrupar: { ...q.agrupar, campos: q.agrupar.campos.filter((_, j) => j !== i) }, orden: "" })} aria-label={`Quitar ${fd?.etiqueta ?? name}`}>
                      <IconX width={12} height={12} />
                    </button>
                  </span>
                );
              })}
              {q.agrupar.campos.length < 3 && (
                <FieldSearch
                  fields={fields.filter((f) => GROUPABLE.includes(f.tipo) && f.agrupable)}
                  exclude={q.agrupar.campos.map((g) => g.split(":")[0])}
                  placeholder="+ Agrupar por…"
                  onPick={(fd) => set({ agrupar: { ...q.agrupar, campos: [...q.agrupar.campos, DATES.includes(fd.tipo) ? `${fd.campo}:month` : fd.campo] } })}
                />
              )}
            </div>
            {grouped && (
              <div>
                <span className="text-xs text-ink-2">Sumar:</span>
                <div className="mt-1 grid max-h-44 grid-cols-1 gap-1 overflow-y-auto rounded-lg border border-line p-2 sm:grid-cols-2 lg:grid-cols-3">
                  {numeric.map((f) => (
                    <label key={f.campo} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={q.agrupar.sumar.includes(f.campo)}
                        onChange={(e) => set({
                          agrupar: { ...q.agrupar, sumar: e.target.checked ? [...q.agrupar.sumar, f.campo] : q.agrupar.sumar.filter((x) => x !== f.campo) },
                        })}
                      />
                      <span className="truncate">{f.etiqueta}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Section>

        {!grouped && (
          <Section n="5" title="Columnas del Excel" hint="En este orden. Puedes cambiar el título de cada columna.">
            <ol className="space-y-1.5">
              {q.columnas.map((c, i) => (
                <li key={c.campo} className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-2 py-1.5">
                  <span className="w-5 text-right text-xs text-muted">{i + 1}</span>
                  <span className="min-w-[8rem] flex-1 truncate text-sm">
                    {label(c.campo)} <span className="font-mono text-[11px] text-muted">{c.campo}</span>
                  </span>
                  <input
                    className="input w-full py-1 text-xs sm:w-48"
                    placeholder="Título en el Excel"
                    value={c.etiqueta}
                    maxLength={60}
                    onChange={(e) => set({ columnas: q.columnas.map((x, j) => (j === i ? { ...x, etiqueta: e.target.value } : x)) })}
                    aria-label={`Título de ${label(c.campo)}`}
                  />
                  <div className="flex">
                    <button className="btn-ghost p-1" onClick={() => moveCol(i, -1)} disabled={i === 0} aria-label="Subir"><IconChevronUp width={14} height={14} /></button>
                    <button className="btn-ghost p-1" onClick={() => moveCol(i, 1)} disabled={i === q.columnas.length - 1} aria-label="Bajar"><IconChevronDown width={14} height={14} /></button>
                    <button className="btn-ghost p-1 text-bad" onClick={() => set({ columnas: q.columnas.filter((_, j) => j !== i), orden: q.orden.startsWith(c.campo) ? "" : q.orden })} aria-label="Quitar columna">
                      <IconX width={14} height={14} />
                    </button>
                  </div>
                </li>
              ))}
            </ol>
            {q.columnas.length < 40 && (
              <div className="mt-2">
                <FieldSearch
                  fields={fields}
                  exclude={q.columnas.map((c) => c.campo)}
                  placeholder="+ Agregar columna: buscar campo…"
                  onPick={(fd) => set({ columnas: [...q.columnas, { campo: fd.campo, etiqueta: "" }] })}
                />
              </div>
            )}
          </Section>
        )}

        <Section n={grouped ? "5" : "6"} title="Orden y quién la ve">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm">
              <span className="text-xs text-ink-2">Ordenar por</span>
              <select className="input" value={ordField ?? ""} onChange={(e) => set({ orden: e.target.value ? `${e.target.value} ${ordDir || "desc"}` : "" })}>
                <option value="">{grouped ? "Primera suma (mayor a menor)" : "Fecha (más reciente primero)"}</option>
                {orderOptions.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
              </select>
              {ordField && (
                <select className="input" value={ordDir || "desc"} onChange={(e) => set({ orden: `${ordField} ${e.target.value}` })} aria-label="Dirección">
                  <option value="desc">Mayor a menor / Z-A</option>
                  <option value="asc">Menor a mayor / A-Z</option>
                </select>
              )}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={q.compartida} onChange={(e) => set({ compartida: e.target.checked })} />
              Compartida con quien tenga acceso a «Datos de Odoo»
            </label>
          </div>
        </Section>

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <button className="btn-primary" onClick={() => preview.mutate(false)} disabled={preview.isPending || !q.fuente}>
            {preview.isPending ? "Consultando…" : "Vista previa"}
          </button>
          <button className="btn-ghost border border-line" onClick={() => excel.mutate()} disabled={excel.isPending || !q.fuente} title={`Hasta ${cap.toLocaleString("es-MX")} registros`}>
            <IconDownload width={16} height={16} /> {excel.isPending ? "Generando…" : "Descargar Excel"}
          </button>
          <span className="mx-1 hidden h-6 w-px bg-line sm:block" aria-hidden />
          {editable && (
            <button className="btn-ghost border border-line" onClick={() => save.mutate(false)} disabled={save.isPending || !q.nombre.trim()}>
              Guardar
            </button>
          )}
          {q.id && (
            <button className="btn-ghost border border-line" onClick={() => save.mutate(true)} disabled={save.isPending}>
              Guardar como copia
            </button>
          )}
          {q.id && editable && (
            <button className="btn-ghost text-bad" onClick={() => window.confirm(`¿Eliminar la consulta «${q.nombre}»?`) && remove.mutate()}>
              Eliminar
            </button>
          )}
          {ok && <span className="inline-flex items-center gap-1 text-sm text-good"><IconCheck width={14} height={14} /> {ok}</span>}
        </div>
        {!editable && <p className="text-xs text-muted">La creó {initial.propietario}: puedes usarla o guardarla como copia para cambiarla.</p>}
        {error && <p role="alert" className="text-sm text-bad">{(error as Error).message}</p>}
      </div>

      {result && <ResultView result={result} onRefresh={() => preview.mutate(true)} refreshing={preview.isPending} />}
    </div>
  );
}

// ------------------------------------------------------------------ pestaña de consultas
function QueriesTab() {
  const srcQ = useQuery({ queryKey: ["datos-fuentes"], queryFn: api.datos.sources, staleTime: 5 * 60_000 });
  const listQ = useQuery({ queryKey: ["datos-consultas"], queryFn: api.datos.queries });
  const [selected, setSelected] = useState<{ key: string; q: DataQuery } | null>(null);
  const sources = srcQ.data?.fuentes ?? [];
  const list = listQ.data?.consultas ?? [];
  const srcLabel = (k: string) => sources.find((s) => s.key === k)?.label ?? k;

  useEffect(() => {
    if (!selected && list.length && sources.length) setSelected({ key: list[0].id as string, q: list[0] });
  }, [selected, list, sources]);

  if (srcQ.error || listQ.error) return <ErrorState message={((srcQ.error || listQ.error) as Error).message} />;
  if (!srcQ.data || !listQ.data) return <div className="skeleton h-64 w-full" />;

  const startNew = () => {
    const first = sources.find((s) => s.disponible);
    setSelected({ key: `nueva-${Date.now()}`, q: emptyQuery(first) });
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="card h-fit p-3">
        <button className="btn-primary mb-3 w-full" onClick={startNew}>
          <IconPlus width={16} height={16} /> Nueva consulta
        </button>
        <ul className="space-y-1" aria-label="Consultas guardadas">
          {list.map((c) => (
            <li key={c.id}>
              <button
                className={`w-full rounded-lg px-2.5 py-2 text-left ${selected?.key === c.id ? "bg-brand/10" : "hover:bg-line/50"}`}
                onClick={() => setSelected({ key: c.id as string, q: c })}
                aria-current={selected?.key === c.id}
              >
                <div className={`truncate text-sm ${selected?.key === c.id ? "font-semibold text-brand" : "font-medium"}`}>{c.nombre}</div>
                <div className="truncate text-xs text-muted">
                  {srcLabel(c.fuente)}
                  {c.agrupar.campos.length ? " · agrupada" : ""}
                  {!c.compartida && " · privada"}
                </div>
              </button>
            </li>
          ))}
        </ul>
        <p className="mt-3 border-t border-line pt-3 text-xs text-muted">
          Solo lectura: el BI nunca modifica Odoo. Los resultados se guardan hasta la actualización nocturna, así que abrir la misma consulta no vuelve a cargar a Odoo.
        </p>
      </aside>
      {selected ? (
        <QueryEditor
          key={selected.key}
          initial={selected.q}
          sources={sources}
          cap={srcQ.data.tope}
          onSaved={(saved) => setSelected({ key: saved.id as string, q: saved })}
          onDeleted={() => setSelected(null)}
        />
      ) : (
        <div className="card p-8 text-center text-sm text-muted">Elige una consulta o crea una nueva.</div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ pestaña de sincronización
function RunStatus({ run }: { run: SyncRun | null | undefined }) {
  if (!run) return <span className="text-xs text-muted">Sin registro todavía</span>;
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${run.ok ? "text-ink-2" : "text-bad"}`}>
      {run.ok ? <IconCheck width={13} height={13} className="text-good" /> : <IconX width={13} height={13} />}
      {run.ok ? "Actualizado" : "Falló"} {fmtTs(run.ts)} · {run.segundos.toLocaleString("es-MX")} s · {run.tipo === "manual" ? "manual" : "nocturna"}
      {run.error && <span className="block max-w-xs truncate" title={run.error}>: {run.error}</span>}
    </span>
  );
}

function SyncTab() {
  const qc = useQueryClient();
  const { data, error } = useQuery({ queryKey: ["datos-sync"], queryFn: api.datos.sync, refetchInterval: 60_000 });
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  useEffect(() => { if (data && chosen === null) setChosen(data.modulos.filter((m) => m.activo).map((m) => m.key)); }, [data, chosen]);
  const save = useMutation({
    mutationFn: () => api.datos.saveSync(chosen ?? []),
    onSuccess: (d) => { qc.setQueryData(["datos-sync"], d); setOk("Guardado: aplica desde la próxima corrida nocturna."); },
  });
  const now = useMutation({
    mutationFn: (m: string) => api.datos.syncNow(m),
    onSuccess: (d, m) => { qc.setQueryData(["datos-sync"], d); qc.invalidateQueries({ queryKey: ["kpis", m] }); setOk("Listo: el módulo ya muestra lo más reciente de Odoo."); },
  });
  if (error) return <ErrorState message={(error as Error).message} />;
  if (!data || !chosen) return <div className="skeleton h-64 w-full" />;
  const dirty = chosen.join() !== data.modulos.filter((m) => m.activo).map((m) => m.key).join();

  return (
    <div className="space-y-4">
      <section className="card p-4">
        <h2 className="text-sm font-semibold">Qué se trae de Odoo cada noche</h2>
        <p className="mt-1 text-xs text-muted">
          {data.hora ? <>Corrida diaria a las <b>{data.hora}</b> (hora de México); la próxima es el {fmtTs(data.siguiente)}.</> : "La corrida nocturna está desactivada."}{" "}
          Un módulo encendido amanece listo. Uno apagado se consulta a Odoo la primera vez que alguien lo abre en el día (y queda guardado para los demás).
        </p>
        <ul className="mt-3 divide-y divide-line">
          {data.modulos.map((m) => (
            <li key={m.key} className="flex flex-wrap items-center gap-3 py-2.5">
              <label className="flex min-w-[13rem] flex-1 items-center gap-2 text-sm font-medium">
                <input
                  type="checkbox"
                  checked={chosen.includes(m.key)}
                  onChange={(e) => { setOk(null); setChosen(e.target.checked ? [...chosen, m.key] : chosen.filter((x) => x !== m.key)); }}
                />
                {m.label}
              </label>
              <div className="min-w-0 flex-[2]"><RunStatus run={m.ultimo} /></div>
              <button className="btn-ghost border border-line px-2 py-1 text-xs" onClick={() => now.mutate(m.key)} disabled={now.isPending}>
                <IconRefresh width={13} height={13} className={now.isPending && now.variables === m.key ? "animate-spin" : ""} />
                {now.isPending && now.variables === m.key ? "Trayendo…" : "Traer ahora"}
              </button>
            </li>
          ))}
          <li className="flex flex-wrap items-center gap-3 py-2.5">
            <span className="flex min-w-[13rem] flex-1 items-center gap-2 text-sm font-medium">
              <input type="checkbox" checked disabled aria-label="Siempre activo" /> Histórico de flota y ocupación
            </span>
            <div className="min-w-0 flex-[2] text-xs text-muted">
              <RunStatus run={data.flota.ultimo} />
              <span className="block">Siempre activo: el cierre de las 23:00 y el tacómetro dependen de él{data.hora_manana ? `; corte de la mañana a las ${data.hora_manana}` : ""}.</span>
            </div>
          </li>
        </ul>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button className="btn-primary" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>Guardar</button>
          {ok && <span className="inline-flex items-center gap-1 text-sm text-good"><IconCheck width={14} height={14} /> {ok}</span>}
          {(save.error || now.error) && <p role="alert" className="text-sm text-bad">{((save.error || now.error) as Error).message}</p>}
        </div>
      </section>
      <section className="card p-4 text-sm">
        <h2 className="text-sm font-semibold">Protecciones para Odoo</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-ink-2">
          <li>Solo lectura: el BI no puede crear, modificar ni borrar nada en Odoo.</li>
          <li>Tope de <b>{data.tope_llamadas_min.toLocaleString("es-MX")}</b> consultas por minuto; si Odoo no responde, el BI deja de insistir unos minutos.</li>
          <li>«Traer ahora» se permite una vez cada 10 minutos por módulo.</li>
          <li>Las consultas de «Datos de Odoo» traen hasta <b>{data.tope_registros.toLocaleString("es-MX")}</b> registros por Excel y solo campos almacenados (los cálculos al vuelo son caros para Odoo).</li>
        </ul>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------ página
export default function DataPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<"consultas" | "sync">("consultas");
  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-2">Arma tus propias tablas con los datos de Odoo (columnas, periodo, reglas y totales) y descárgalas en Excel.</p>
      {user?.is_admin && (
        <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
          {([["consultas", "Consultas"], ["sync", "Sincronización"]] as const).map(([k, l]) => (
            <button
              key={k}
              role="tab"
              aria-selected={tab === k}
              onClick={() => setTab(k)}
              className={`-mb-px border-b-2 px-3 py-2 text-sm ${tab === k ? "border-brand font-medium text-brand" : "border-transparent text-ink-2 hover:text-ink"}`}
            >
              {l}
            </button>
          ))}
        </div>
      )}
      {tab === "consultas" || !user?.is_admin ? <QueriesTab /> : <SyncTab />}
    </div>
  );
}
