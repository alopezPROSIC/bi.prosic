import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { ErrorState } from "../components/States";
import { useFilterOptions } from "../hooks/useKpis";
import type {
  CommissionAccount,
  CommissionDepuracion,
  CommissionMonth,
  CommissionPlan,
  CommissionPuesto,
  CommissionRule,
  CommissionRuleAction,
  CommissionTramo,
  FilterOption,
} from "../types/api";

// Fase 02 (plan y metas), Fase 04 (cálculo manual de Finanzas para comparar) y Fase 05 (estados de cuenta).

function Section({ title, actions, children, hint }: { title: string; actions?: ReactNode; children: ReactNode; hint?: ReactNode }) {
  return (
    <section className="card min-w-0 p-4">
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions}
      </header>
      {hint && <p className="mb-3 text-xs text-muted">{hint}</p>}
      {children}
    </section>
  );
}

function Feedback({ error, ok }: { error?: unknown; ok?: string | null }) {
  if (error) return <p role="alert" className="text-sm text-bad">{(error as Error).message}</p>;
  if (ok) return <p className="text-sm text-good">{ok}</p>;
  return null;
}

function Num({ value, onChange, placeholder, className = "w-28", step = "any" }: {
  value: number | undefined | null;
  onChange: (v: number | undefined) => void;
  placeholder?: string;
  className?: string;
  step?: string;
}) {
  return (
    <input
      type="number"
      inputMode="decimal"
      step={step}
      min={0}
      className={`input text-right ${className}`}
      value={value ?? ""}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
    />
  );
}

function thisMonth(offset = 0) {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function prevMonth(m: string) {
  const [y, mm] = m.split("-").map(Number);
  return mm === 1 ? `${y - 1}-12` : `${y}-${String(mm - 1).padStart(2, "0")}`;
}

// ------------------------------------------------------------------ tramos
function TramosEditor({ tramos, onChange, desdeLabel, valorLabel, valorStep = "0.01" }: {
  tramos: CommissionTramo[];
  onChange: (t: CommissionTramo[]) => void;
  desdeLabel: string;
  valorLabel: string;
  valorStep?: string;
}) {
  const set = (i: number, patch: Partial<CommissionTramo>) => onChange(tramos.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  return (
    <div className="space-y-1">
      <div className="grid grid-cols-[7rem_7rem_auto] gap-2 text-xs text-ink-2">
        <span>{desdeLabel}</span>
        <span>{valorLabel}</span>
      </div>
      {tramos.map((t, i) => (
        <div key={i} className="grid grid-cols-[7rem_7rem_auto] items-center gap-2">
          <Num value={t.desde} onChange={(v) => set(i, { desde: v ?? 0 })} className="w-28" />
          <Num value={t.valor} onChange={(v) => set(i, { valor: v ?? 0 })} className="w-28" step={valorStep} />
          <button className="btn-ghost justify-self-start text-xs text-bad" onClick={() => onChange(tramos.filter((_, j) => j !== i))}>
            Quitar
          </button>
        </div>
      ))}
      <button
        className="btn-ghost border border-line text-xs"
        onClick={() => onChange([...tramos, { desde: tramos.length ? Math.max(...tramos.map((t) => t.desde)) + 10 : 0, valor: 0 }])}
      >
        + Tramo
      </button>
    </div>
  );
}

// ------------------------------------------------------------------ plan (Fase 02)
function PlanEditor({ initial, onSaved }: { initial: CommissionPlan; onSaved: () => void }) {
  const { data: opts } = useFilterOptions();
  const [p, setP] = useState<CommissionPlan>(initial);
  const [ok, setOk] = useState<string | null>(null);
  useEffect(() => setP(initial), [initial]);
  const set = (patch: Partial<CommissionPlan>) => {
    setOk(null);
    setP((x) => ({ ...x, ...patch }));
  };
  const save = useMutation({
    mutationFn: () => api.comisiones.savePlan(p),
    onSuccess: () => {
      setOk("Plan guardado. El módulo Comisiones ya calcula con estos datos.");
      onSaved();
    },
  });
  const sellers = useMutation({
    mutationFn: api.comisiones.odooSellers,
    onSuccess: (d) => {
      const have = new Set(p.vendedores.map((v) => v.odoo_id));
      const def = p.puestos.find((x) => x.base === "propia")?.key ?? p.puestos[0].key;
      const add = d.vendedores.filter((v) => !have.has(v.odoo_id)).map((v) => ({
        odoo_id: v.odoo_id, nombre: v.nombre, puesto: def, sucursal_id: null, correo: "", activo: true,
      }));
      set({ vendedores: [...p.vendedores, ...add].sort((a, b) => a.nombre.localeCompare(b.nombre)) });
      setOk(add.length ? `Se agregaron ${add.length} vendedores de Odoo. Revisa su puesto y sucursal, y guarda.` : "No hay vendedores nuevos en Odoo.");
    },
  });
  const setPuesto = (i: number, patch: Partial<CommissionPuesto>) => set({ puestos: p.puestos.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
  const branches: FilterOption[] = opts?.branches ?? [];
  const sucursal = (bid: number) => p.sucursales.find((s) => s.branch_id === bid);
  const setAlmacenes = (b: FilterOption, ids: number[]) => {
    const rest = p.sucursales.filter((s) => s.branch_id !== b.id);
    set({ sucursales: ids.length ? [...rest, { branch_id: b.id, nombre: b.name, almacenes: ids }] : rest });
  };

  return (
    <div className="space-y-4">
      <Section
        title="1 · Puestos y tabulador"
        hint="El % de comisión se aplica sobre la base cobrada del mes según el % de cumplimiento de la meta (aplica el tramo más alto alcanzado). Un puesto «por sucursal» (gerente) comisiona sobre lo cobrado por toda su sucursal contra la meta de la sucursal."
        actions={
          <button
            className="btn-ghost border border-line text-xs"
            onClick={() => set({ puestos: [...p.puestos, { key: `puesto_${p.puestos.length + 1}`, label: "Nuevo puesto", base: "propia", tramos: [] }] })}
          >
            + Puesto
          </button>
        }
      >
        <div className="grid gap-4 lg:grid-cols-3">
          {p.puestos.map((x, i) => (
            <div key={i} className="space-y-2 rounded-lg border border-line p-3">
              <input className="input w-full font-medium" value={x.label} onChange={(e) => setPuesto(i, { label: e.target.value })} />
              <select className="input w-full" value={x.base} onChange={(e) => setPuesto(i, { base: e.target.value as "propia" | "sucursal" })}>
                <option value="propia">Comisiona sobre su base propia</option>
                <option value="sucursal">Comisiona sobre toda su sucursal</option>
              </select>
              <TramosEditor tramos={x.tramos} onChange={(t) => setPuesto(i, { tramos: t })} desdeLabel="Desde % de meta" valorLabel="% comisión" />
              {!x.tramos.length && <p className="text-xs text-warn">Sin tramos: este puesto no comisiona hasta capturarlos.</p>}
              {p.puestos.length > 1 && !p.vendedores.some((v) => v.puesto === x.key) && (
                <button className="btn-ghost text-xs text-bad" onClick={() => set({ puestos: p.puestos.filter((_, j) => j !== i) })}>
                  Eliminar puesto
                </button>
              )}
            </div>
          ))}
        </div>
      </Section>

      <Section
        title="2 · Factor por ocupación física (opcional)"
        hint="Multiplica la comisión según el % de ocupación promedio del mes de la sucursal (Histórico de flota, en los almacenes asignados abajo). Ej.: desde 0 % → 0.8, desde 70 % → 1.0."
      >
        <label className="mb-3 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={p.ocupacion.activo} onChange={(e) => set({ ocupacion: { ...p.ocupacion, activo: e.target.checked } })} />
          Aplicar factor por ocupación
        </label>
        {p.ocupacion.activo && (
          <TramosEditor
            tramos={p.ocupacion.tramos}
            onChange={(t) => set({ ocupacion: { ...p.ocupacion, tramos: t } })}
            desdeLabel="Desde % ocupación"
            valorLabel="Factor"
          />
        )}
      </Section>

      <Section title="3 · Almacenes de cada sucursal" hint="Solo se usan para medir la ocupación del factor anterior.">
        <div className="grid gap-2 md:grid-cols-2">
          {branches.map((b) => {
            const s = sucursal(b.id);
            return (
              <details key={b.id} className="rounded-lg border border-line p-2">
                <summary className="cursor-pointer text-sm">
                  {b.name} <span className="text-xs text-muted">· {s?.almacenes.length ?? 0} almacén(es)</span>
                </summary>
                <div className="mt-2 grid max-h-48 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2">
                  {(opts?.warehouses ?? []).map((w) => (
                    <label key={w.id} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={!!s?.almacenes.includes(w.id)}
                        onChange={(e) => {
                          const cur = s?.almacenes ?? [];
                          setAlmacenes(b, e.target.checked ? [...cur, w.id] : cur.filter((x) => x !== w.id));
                        }}
                      />
                      <span className="truncate">{w.name}</span>
                    </label>
                  ))}
                </div>
              </details>
            );
          })}
          {!branches.length && <p className="text-xs text-muted">No hay sucursales en Odoo.</p>}
        </div>
      </Section>

      <Section
        title={`4 · Vendedores (${p.vendedores.length})`}
        hint="Son los «Vendedor» de las facturas en Odoo. El correo se usa para enviarle su estado de cuenta (Fase 05)."
        actions={
          <button className="btn-ghost border border-line text-xs" disabled={sellers.isPending} onClick={() => sellers.mutate()}>
            {sellers.isPending ? "Consultando Odoo…" : "Traer vendedores de Odoo"}
          </button>
        }
      >
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                <th className="px-2 py-2 font-medium">Vendedor</th>
                <th className="px-2 py-2 font-medium">Puesto</th>
                <th className="px-2 py-2 font-medium">Sucursal</th>
                <th className="px-2 py-2 font-medium">Correo</th>
                <th className="px-2 py-2 font-medium">Activo</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {p.vendedores.map((v, i) => {
                const upd = (patch: Partial<typeof v>) => set({ vendedores: p.vendedores.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
                return (
                  <tr key={v.odoo_id} className="border-b border-line/60 last:border-0">
                    <td className="px-2 py-1">{v.nombre}</td>
                    <td className="px-2 py-1">
                      <select className="input" value={v.puesto} onChange={(e) => upd({ puesto: e.target.value })}>
                        {p.puestos.map((x) => (
                          <option key={x.key} value={x.key}>{x.label}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1">
                      <select className="input" value={v.sucursal_id ?? ""} onChange={(e) => upd({ sucursal_id: e.target.value ? Number(e.target.value) : null })}>
                        <option value="">— Según sus facturas —</option>
                        {branches.map((b) => (
                          <option key={b.id} value={b.id}>{b.name}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-2 py-1">
                      <input className="input w-56" type="email" value={v.correo} placeholder="nombre@prosic.com" onChange={(e) => upd({ correo: e.target.value })} />
                    </td>
                    <td className="px-2 py-1 text-center">
                      <input type="checkbox" checked={v.activo} onChange={(e) => upd({ activo: e.target.checked })} />
                    </td>
                    <td className="px-2 py-1">
                      <button className="btn-ghost text-xs text-bad" onClick={() => set({ vendedores: p.vendedores.filter((_, j) => j !== i) })}>
                        Quitar
                      </button>
                    </td>
                  </tr>
                );
              })}
              {!p.vendedores.length && (
                <tr>
                  <td colSpan={6} className="px-2 py-6 text-center text-muted">Usa «Traer vendedores de Odoo» para empezar.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {p.vendedores.some((v) => p.puestos.find((x) => x.key === v.puesto)?.base === "sucursal" && !v.sucursal_id) && (
          <p className="mt-2 text-xs text-warn">Los gerentes deben tener sucursal asignada.</p>
        )}
      </Section>

      <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface p-3 shadow-sm">
        <button className="btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Guardando…" : "Guardar plan"}
        </button>
        <button className="btn-ghost border border-line" onClick={() => setP(initial)}>Descartar cambios</button>
        <Feedback error={save.error || sellers.error} ok={ok} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ datos del mes (metas, Fase 04, ajustes)
const EMPTY_MONTH: CommissionMonth = { metas_vendedor: {}, metas_sucursal: {}, manual: {}, ajustes: [] };

function clean(d: CommissionMonth): CommissionMonth {
  const strip = (r: Record<string, number>) => Object.fromEntries(Object.entries(r).filter(([, v]) => v !== undefined && v !== null && !Number.isNaN(v)));
  return { ...d, metas_vendedor: strip(d.metas_vendedor), metas_sucursal: strip(d.metas_sucursal), manual: strip(d.manual) };
}

function MonthEditor({ plan, mes }: { plan: CommissionPlan; mes: string }) {
  const qc = useQueryClient();
  const { data: opts } = useFilterOptions();
  const { data, isLoading, error } = useQuery({ queryKey: ["comis-mes", mes], queryFn: () => api.comisiones.month(mes) });
  const [d, setD] = useState<CommissionMonth>(EMPTY_MONTH);
  const [ok, setOk] = useState<string | null>(null);
  useEffect(() => setD(data?.datos ?? EMPTY_MONTH), [data]);
  const save = useMutation({
    mutationFn: () => api.comisiones.saveMonth(mes, clean(d)),
    onSuccess: () => {
      setOk("Datos del mes guardados.");
      qc.invalidateQueries({ queryKey: ["comis-mes", mes] });
    },
  });
  const copyPrev = useMutation({
    mutationFn: () => api.comisiones.month(prevMonth(mes)),
    onSuccess: (r) => {
      setD((x) => ({ ...x, metas_vendedor: { ...r.datos.metas_vendedor }, metas_sucursal: { ...r.datos.metas_sucursal } }));
      setOk(`Metas copiadas de ${prevMonth(mes)}. Revisa y guarda.`);
    },
  });
  const active = plan.vendedores.filter((v) => v.activo);
  const managerBranches = useMemo(() => {
    const ids = new Set(plan.vendedores.filter((v) => plan.puestos.find((p) => p.key === v.puesto)?.base === "sucursal").map((v) => v.sucursal_id));
    return (opts?.branches ?? []).filter((b) => ids.has(b.id) || d.metas_sucursal[b.id] !== undefined);
  }, [plan, opts, d.metas_sucursal]);
  const setMap = (k: "metas_vendedor" | "metas_sucursal" | "manual", id: number, v: number | undefined) => {
    setOk(null);
    setD((x) => {
      const m = { ...x[k] };
      if (v === undefined) delete m[id];
      else m[id] = v;
      return { ...x, [k]: m };
    });
  };
  if (error) return <ErrorState message={(error as Error).message} />;
  if (isLoading) return <div className="skeleton h-40 w-full" />;
  return (
    <div className="space-y-4">
      <Section
        title="Metas del mes y cálculo manual de Finanzas"
        hint="Meta = base cobrada esperada en el mes (sin IVA). «Cálculo manual» es la comisión que Finanzas calculó por su cuenta: el módulo Comisiones muestra la diferencia contra el cálculo del BI (Fase 04). Déjalo vacío si no aplica."
        actions={
          <button className="btn-ghost border border-line text-xs" disabled={copyPrev.isPending} onClick={() => copyPrev.mutate()}>
            Copiar metas de {prevMonth(mes)}
          </button>
        }
      >
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                <th className="px-2 py-2 font-medium">Vendedor</th>
                <th className="px-2 py-2 font-medium">Puesto</th>
                <th className="px-2 py-2 text-right font-medium">Meta del mes</th>
                <th className="px-2 py-2 text-right font-medium">Cálculo manual (Finanzas)</th>
              </tr>
            </thead>
            <tbody>
              {active.map((v) => {
                const p = plan.puestos.find((x) => x.key === v.puesto);
                return (
                  <tr key={v.odoo_id} className="border-b border-line/60 last:border-0">
                    <td className="px-2 py-1">{v.nombre}</td>
                    <td className="px-2 py-1 text-xs text-ink-2">{p?.label}{p?.base === "sucursal" ? " (usa la meta de su sucursal si no tiene propia)" : ""}</td>
                    <td className="px-2 py-1 text-right">
                      <Num value={d.metas_vendedor[v.odoo_id]} onChange={(x) => setMap("metas_vendedor", v.odoo_id, x)} className="w-36" placeholder="$" />
                    </td>
                    <td className="px-2 py-1 text-right">
                      <Num value={d.manual[v.odoo_id]} onChange={(x) => setMap("manual", v.odoo_id, x)} className="w-36" placeholder="$" />
                    </td>
                  </tr>
                );
              })}
              {!active.length && (
                <tr>
                  <td colSpan={4} className="px-2 py-6 text-center text-muted">Primero da de alta vendedores en «Plan de comisiones».</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {managerBranches.length > 0 && (
          <div className="mt-4">
            <h3 className="mb-1 text-xs font-semibold text-ink-2">Metas por sucursal (para gerentes)</h3>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {managerBranches.map((b) => (
                <label key={b.id} className="flex items-center justify-between gap-2 text-sm">
                  <span className="truncate">{b.name}</span>
                  <Num value={d.metas_sucursal[b.id]} onChange={(x) => setMap("metas_sucursal", b.id, x)} className="w-36" placeholder="$" />
                </label>
              ))}
            </div>
          </div>
        )}
      </Section>

      <Section
        title={`Ajustes del mes (${d.ajustes.length})`}
        hint="Montos que se suman (o restan, con signo negativo) a la comisión calculada, con su motivo. Quedan en la bitácora."
        actions={
          active.length ? (
            <button
              className="btn-ghost border border-line text-xs"
              onClick={() => setD((x) => ({ ...x, ajustes: [...x.ajustes, { odoo_id: active[0].odoo_id, monto: 0, motivo: "" }] }))}
            >
              + Ajuste
            </button>
          ) : null
        }
      >
        <div className="space-y-2">
          {d.ajustes.map((a, i) => {
            const upd = (patch: Partial<typeof a>) => setD((x) => ({ ...x, ajustes: x.ajustes.map((y, j) => (j === i ? { ...y, ...patch } : y)) }));
            return (
              <div key={i} className="flex flex-wrap items-center gap-2">
                <select className="input" value={a.odoo_id} onChange={(e) => upd({ odoo_id: Number(e.target.value) })}>
                  {plan.vendedores.map((v) => (
                    <option key={v.odoo_id} value={v.odoo_id}>{v.nombre}</option>
                  ))}
                </select>
                <input
                  type="number"
                  step="any"
                  className="input w-32 text-right"
                  value={a.monto}
                  onChange={(e) => upd({ monto: Number(e.target.value) })}
                />
                <input className="input min-w-[16rem] flex-1" placeholder="Motivo" value={a.motivo} onChange={(e) => upd({ motivo: e.target.value })} />
                <button className="btn-ghost text-xs text-bad" onClick={() => setD((x) => ({ ...x, ajustes: x.ajustes.filter((_, j) => j !== i) }))}>
                  Quitar
                </button>
              </div>
            );
          })}
        </div>
      </Section>

      <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface p-3 shadow-sm">
        <button className="btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Guardando…" : `Guardar ${mes}`}
        </button>
        {data?.updated_at && <span className="text-xs text-muted">Última edición: {data.updated_by} · {new Date(data.updated_at).toLocaleString("es-MX")}</span>}
        <Feedback error={save.error || copyPrev.error} ok={ok} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ estados de cuenta (Fase 05)
function Statements({ mes }: { mes: string }) {
  const { data, error } = useQuery({ queryKey: ["comis-envios", mes], queryFn: () => api.comisiones.sending(mes) });
  const [ok, setOk] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<unknown>(null);
  const send = useMutation({
    mutationFn: (id?: number) => api.comisiones.send(mes, id),
    onSuccess: (r) =>
      setOk(`Enviados: ${r.enviados.length}.` + (r.omitidos.length ? ` Omitidos: ${r.omitidos.map((o) => `${o.nombre} (${o.motivo})`).join(", ")}.` : "")),
  });
  if (error) return <ErrorState message={(error as Error).message} />;
  return (
    <Section
      title={`Estados de cuenta de ${mes}`}
      hint={
        <>
          Cada vendedor recibe su cálculo y sus facturas cobradas; cada gerente, además, el de su equipo.{" "}
          {data?.automatico
            ? `Envío automático: el día ${data.dia} de cada mes (mes anterior).`
            : "El envío automático está apagado (se activa en Configuración cuando Finanzas valide el cálculo)."}
          {data && !data.smtp && " El correo (SMTP) aún no está configurado: solo hay vista previa."}
        </>
      }
      actions={
        <button
          className="btn-primary text-xs"
          disabled={!data?.smtp || send.isPending || !data.destinatarios.length}
          onClick={() => window.confirm(`¿Enviar el estado de cuenta de ${mes} a ${data?.destinatarios.length} persona(s)?`) && send.mutate(undefined)}
        >
          Enviar a todos
        </button>
      }
    >
      <ul className="divide-y divide-line/60 text-sm">
        {(data?.destinatarios ?? []).map((r) => (
          <li key={r.odoo_id} className="flex flex-wrap items-center gap-2 py-1.5">
            <span className="min-w-[12rem] flex-1">
              {r.nombre} {r.gerente && <span className="rounded bg-brand/10 px-1.5 text-xs text-brand">Gerente</span>}
              <span className="block text-xs text-muted">{r.correo}</span>
            </span>
            <button
              className="btn-ghost border border-line text-xs"
              onClick={() => {
                setPreviewError(null);
                api.comisiones.openStatement(mes, r.odoo_id).catch(setPreviewError);
              }}
            >
              Vista previa
            </button>
            <button className="btn-ghost border border-line text-xs" disabled={!data?.smtp || send.isPending} onClick={() => send.mutate(r.odoo_id)}>
              Enviar
            </button>
          </li>
        ))}
        {data && !data.destinatarios.length && <li className="py-4 text-center text-muted">Ningún vendedor activo tiene correo capturado.</li>}
      </ul>
      <div className="mt-2">
        <Feedback error={send.error || previewError} ok={ok} />
      </div>
    </Section>
  );
}

// ------------------------------------------------------------------ reglas y depuración
const ACTION_LABEL: Record<CommissionRuleAction, string> = {
  reasignar: "Traspasar comisión",
  excluir: "Excluir (no comisiona)",
  incluir: "Incluir aunque se haya excluido",
};
const ACTION_HINT: Record<CommissionRuleAction, string> = {
  reasignar: "Las facturas que cumplan las condiciones comisionan para otro vendedor (o sucursal). Ej.: baja de personal, cartera reasignada, cuenta compartida al 50 %.",
  excluir: "Las facturas que cumplan las condiciones no comisionan. Ej.: cliente intercompañía, operación irregular, factura en revisión.",
  incluir: "Rescata facturas excluidas por la depuración automática (póliza mixta, cobro tardío). No rescata cancelaciones con nota de crédito ni pólizas sin cobro.",
};

function newRule(accion: CommissionRuleAction): CommissionRule {
  return {
    id: Math.random().toString(36).slice(2, 10).padEnd(8, "0"),
    activo: true,
    accion,
    motivo: "",
    vendedor_id: null,
    sucursal_id: null,
    cliente: "",
    centro_costo: "",
    facturas: [],
    desde: null,
    hasta: null,
    destino_vendedor_id: null,
    destino_sucursal_id: null,
    porcentaje: 100,
  };
}

function AccountChecks({ accounts, value, onChange }: { accounts: CommissionAccount[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
      {accounts.map((a) => (
        <label key={a.code} className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={value.includes(a.code)}
            onChange={(e) => onChange(e.target.checked ? [...value, a.code] : value.filter((c) => c !== a.code))}
          />
          <span>
            <span className="font-mono text-xs">{a.code}</span> {a.nombre}
          </span>
        </label>
      ))}
    </div>
  );
}

function RulesEditor({ initial, onSaved }: { initial: CommissionPlan; onSaved: () => void }) {
  const { data: opts } = useFilterOptions();
  const { data: accData } = useQuery({ queryKey: ["comis-cuentas"], queryFn: api.comisiones.accounts, staleTime: 3600_000 });
  const { data: odooSellers } = useQuery({ queryKey: ["comis-vend-odoo"], queryFn: api.comisiones.odooSellers, staleTime: 3600_000 });
  const [p, setP] = useState<CommissionPlan>(initial);
  const [ok, setOk] = useState<string | null>(null);
  const [desde, setDesde] = useState(thisMonth(-1));
  const [hasta, setHasta] = useState(thisMonth(-1));
  useEffect(() => setP(initial), [initial]);
  const d = p.depuracion;
  const setD = (patch: Partial<CommissionDepuracion>) => {
    setOk(null);
    setP((x) => ({ ...x, depuracion: { ...x.depuracion, ...patch } }));
  };
  const setRule = (i: number, patch: Partial<CommissionRule>) => setD({ reglas: d.reglas.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const move = (i: number, by: number) => {
    const r = [...d.reglas];
    const j = i + by;
    if (j < 0 || j >= r.length) return;
    [r[i], r[j]] = [r[j], r[i]];
    setD({ reglas: r });
  };
  const save = useMutation({
    mutationFn: () => api.comisiones.savePlan(p),
    onSuccess: () => {
      setOk("Reglas guardadas. El módulo Comisiones y los estados de cuenta ya las aplican (sin volver a consultar Odoo).");
      onSaved();
    },
  });
  const excel = useMutation({ mutationFn: () => api.comisiones.downloadExcel(desde, hasta) });
  const income = (accData?.cuentas ?? []).filter((a) => !a.nota_credito);
  const branches: FilterOption[] = opts?.branches ?? [];
  // vendedor original: los de Odoo (incluye bajas) más los del plan
  const sourceSellers = useMemo(() => {
    const m = new Map<number, string>();
    p.vendedores.forEach((v) => m.set(v.odoo_id, v.nombre + (v.activo ? "" : " (inactivo)")));
    (odooSellers?.vendedores ?? []).forEach((v) => !m.has(v.odoo_id) && m.set(v.odoo_id, v.nombre));
    return [...m.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [p.vendedores, odooSellers]);
  const targets = p.vendedores.filter((v) => v.activo);

  return (
    <div className="space-y-4">
      <Section
        title="1 · Cuentas que comisionan"
        hint="Solo el ingreso (sin IVA) de las cuentas marcadas forma la base de comisión. Si no marcas ninguna, comisionan todas las cuentas de la lista de Finanzas."
      >
        {income.length ? (
          <AccountChecks accounts={income} value={d.cuentas} onChange={(c) => setD({ cuentas: c })} />
        ) : (
          <div className="skeleton h-24 w-full" />
        )}
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-2">Notas de crédito registradas en otras cuentas (p. ej. 402-001-001 Renta - NC)</span>
            <select className="input w-full" value={d.notas_credito} onChange={(e) => setD({ notas_credito: e.target.value as CommissionDepuracion["notas_credito"] })}>
              <option value="prorrateo">Descontar la parte proporcional al ingreso que comisiona</option>
              <option value="todas">Descontarlas completas</option>
              <option value="ninguna">No descontarlas</option>
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-ink-2">Días máximos entre la emisión y el cobro total</span>
            <Num value={d.max_dias} onChange={(v) => setD({ max_dias: Math.round(v ?? 365) })} step="1" />
          </label>
        </div>
        <details className="mt-4 rounded-lg border border-line p-3">
          <summary className="cursor-pointer text-sm">Cuentas distintas por puesto (opcional, p. ej. gerentes sobre todo)</summary>
          <div className="mt-3 space-y-3">
            {p.puestos.map((x, i) => (
              <div key={x.key} className="space-y-2">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <input
                    type="checkbox"
                    checked={!!x.cuentas?.length}
                    onChange={(e) => {
                      setOk(null);
                      setP((pp) => ({
                        ...pp,
                        puestos: pp.puestos.map((y, j) => (j === i ? { ...y, cuentas: e.target.checked ? [...(d.cuentas.length ? d.cuentas : income.map((a) => a.code))] : null } : y)),
                      }));
                    }}
                  />
                  {x.label}: {x.cuentas?.length ? "cuentas propias" : "usa las cuentas generales"}
                </label>
                {!!x.cuentas?.length && (
                  <AccountChecks
                    accounts={income}
                    value={x.cuentas}
                    onChange={(c) => setP((pp) => ({ ...pp, puestos: pp.puestos.map((y, j) => (j === i ? { ...y, cuentas: c.length ? c : null } : y)) }))}
                  />
                )}
              </div>
            ))}
          </div>
        </details>
      </Section>

      <Section
        title={`2 · Reglas de depuración (${d.reglas.filter((r) => r.activo).length} activas)`}
        hint="Se evalúan sobre los datos originales de Odoo (vendedor y sucursal de la factura, cliente, centro de costo, folio y fecha de cobro). Las condiciones vacías no filtran. Si varias reglas del mismo tipo coinciden, aplica la primera de la lista. Cada cambio queda en la bitácora."
        actions={
          <div className="flex flex-wrap gap-2">
            {(["reasignar", "excluir", "incluir"] as CommissionRuleAction[]).map((a) => (
              <button key={a} className="btn-ghost border border-line text-xs" onClick={() => setD({ reglas: [...d.reglas, newRule(a)] })}>
                + {a === "reasignar" ? "Traspaso" : a === "excluir" ? "Exclusión" : "Inclusión"}
              </button>
            ))}
          </div>
        }
      >
        {!d.reglas.length && <p className="py-4 text-center text-sm text-muted">Sin reglas: se usa la depuración automática (cobro al 100 %, sin pólizas ni cancelaciones, días máximos).</p>}
        <div className="space-y-3">
          {d.reglas.map((r, i) => (
            <div key={r.id} className={`space-y-3 rounded-lg border p-3 ${r.activo ? "border-line" : "border-dashed border-line opacity-60"}`}>
              <div className="flex flex-wrap items-center gap-2">
                <input type="checkbox" title="Activa" checked={r.activo} onChange={(e) => setRule(i, { activo: e.target.checked })} />
                <select className="input" value={r.accion} onChange={(e) => setRule(i, { accion: e.target.value as CommissionRuleAction })}>
                  {(Object.keys(ACTION_LABEL) as CommissionRuleAction[]).map((a) => (
                    <option key={a} value={a}>{ACTION_LABEL[a]}</option>
                  ))}
                </select>
                <input
                  className="input min-w-[14rem] flex-1"
                  placeholder="Motivo (obligatorio, aparece en el Excel y en la bitácora)"
                  value={r.motivo}
                  onChange={(e) => setRule(i, { motivo: e.target.value })}
                />
                <button className="btn-ghost text-xs" title="Subir" onClick={() => move(i, -1)}>↑</button>
                <button className="btn-ghost text-xs" title="Bajar" onClick={() => move(i, 1)}>↓</button>
                <button className="btn-ghost text-xs text-bad" onClick={() => setD({ reglas: d.reglas.filter((_, j) => j !== i) })}>Quitar</button>
              </div>
              <p className="text-xs text-muted">{ACTION_HINT[r.accion]}</p>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                <label className="text-xs text-ink-2">
                  Vendedor en Odoo
                  <select className="input mt-1 w-full" value={r.vendedor_id ?? ""} onChange={(e) => setRule(i, { vendedor_id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">Cualquiera</option>
                    {sourceSellers.map(([id, n]) => (
                      <option key={id} value={id}>{n}</option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-ink-2">
                  Sucursal de la factura
                  <select className="input mt-1 w-full" value={r.sucursal_id ?? ""} onChange={(e) => setRule(i, { sucursal_id: e.target.value ? Number(e.target.value) : null })}>
                    <option value="">Cualquiera</option>
                    {branches.map((b) => (
                      <option key={b.id} value={b.id}>{b.name}</option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-ink-2">
                  Cobrada desde
                  <input type="date" className="input mt-1 w-full" value={r.desde ?? ""} onChange={(e) => setRule(i, { desde: e.target.value || null })} />
                </label>
                <label className="text-xs text-ink-2">
                  Cobrada hasta
                  <input type="date" className="input mt-1 w-full" value={r.hasta ?? ""} min={r.desde ?? undefined} onChange={(e) => setRule(i, { hasta: e.target.value || null })} />
                </label>
                <label className="text-xs text-ink-2">
                  Cliente contiene
                  <input className="input mt-1 w-full" value={r.cliente} onChange={(e) => setRule(i, { cliente: e.target.value })} />
                </label>
                <label className="text-xs text-ink-2">
                  Centro de costo contiene
                  <input className="input mt-1 w-full" value={r.centro_costo} onChange={(e) => setRule(i, { centro_costo: e.target.value })} />
                </label>
                <label className="text-xs text-ink-2 lg:col-span-2">
                  Folios de factura (separados por coma o renglón)
                  <textarea
                    className="input mt-1 h-9 w-full"
                    value={r.facturas.join(", ")}
                    onChange={(e) => setRule(i, { facturas: e.target.value.split(/[,\n]/).map((x) => x.trim()).filter(Boolean) })}
                  />
                </label>
              </div>
              {r.accion === "reasignar" && (
                <div className="grid gap-2 rounded-lg bg-brand/5 p-2 sm:grid-cols-3">
                  <label className="text-xs text-ink-2">
                    Traspasar a (vendedor)
                    <select className="input mt-1 w-full" value={r.destino_vendedor_id ?? ""} onChange={(e) => setRule(i, { destino_vendedor_id: e.target.value ? Number(e.target.value) : null })}>
                      <option value="">— Mismo vendedor —</option>
                      {targets.map((v) => (
                        <option key={v.odoo_id} value={v.odoo_id}>{v.nombre}</option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs text-ink-2">
                    Sucursal destino
                    <select className="input mt-1 w-full" value={r.destino_sucursal_id ?? ""} onChange={(e) => setRule(i, { destino_sucursal_id: e.target.value ? Number(e.target.value) : null })}>
                      <option value="">La del vendedor destino</option>
                      {branches.map((b) => (
                        <option key={b.id} value={b.id}>{b.name}</option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs text-ink-2">
                    % que se traspasa (el resto se queda con el original)
                    <Num value={r.porcentaje} onChange={(v) => setRule(i, { porcentaje: v ?? 100 })} className="mt-1 w-full" />
                  </label>
                </div>
              )}
            </div>
          ))}
        </div>
      </Section>

      <Section
        title="3 · Excel depurado"
        hint="Incluye el resumen por vendedor, cada factura con el vendedor en Odoo y a quién comisiona, la regla aplicada y el desglose por cuenta, más las reglas y parámetros vigentes. Usa las reglas guardadas."
      >
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-ink-2">
            Desde
            <input type="month" className="input mt-1 block" value={desde} max={thisMonth()} onChange={(e) => e.target.value && setDesde(e.target.value)} />
          </label>
          <label className="text-xs text-ink-2">
            Hasta
            <input type="month" className="input mt-1 block" value={hasta} min={desde} max={thisMonth()} onChange={(e) => e.target.value && setHasta(e.target.value)} />
          </label>
          <button className="btn-primary text-sm" disabled={excel.isPending || hasta < desde} onClick={() => excel.mutate()}>
            {excel.isPending ? "Generando… (puede tardar si el mes no está en caché)" : "Descargar Excel"}
          </button>
        </div>
        <div className="mt-2">
          <Feedback error={excel.error} />
        </div>
      </Section>

      <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface p-3 shadow-sm">
        <button className="btn-primary" disabled={save.isPending || d.reglas.some((r) => r.motivo.trim().length < 3)} onClick={() => save.mutate()}>
          {save.isPending ? "Guardando…" : "Guardar reglas"}
        </button>
        <button className="btn-ghost border border-line" onClick={() => setP(initial)}>Descartar cambios</button>
        {d.reglas.some((r) => r.motivo.trim().length < 3) && <span className="text-xs text-warn">Cada regla necesita un motivo.</span>}
        <Feedback error={save.error} ok={ok} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ pestaña
type Sub = "plan" | "reglas" | "mes" | "envios";

export default function CommissionAdmin() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({ queryKey: ["comis-plan"], queryFn: api.comisiones.plan });
  const [sub, setSub] = useState<Sub>(() => {
    const s = new URLSearchParams(window.location.search).get("sub");
    return s === "reglas" || s === "mes" || s === "envios" ? s : "plan";
  });
  const [mes, setMes] = useState(thisMonth(-1));
  if (error) return <ErrorState message={(error as Error).message} />;
  if (isLoading || !data) return <div className="skeleton h-64 w-full" />;
  const subs: { key: Sub; label: string }[] = [
    { key: "plan", label: "Plan de comisiones" },
    { key: "reglas", label: "Reglas y depuración" },
    { key: "mes", label: "Metas y comparativo del mes" },
    { key: "envios", label: "Estados de cuenta" },
  ];
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {subs.map((s) => (
          <button
            key={s.key}
            onClick={() => setSub(s.key)}
            className={`rounded-full border px-3 py-1 text-sm ${sub === s.key ? "border-brand bg-brand/10 font-medium text-brand" : "border-line text-ink-2"}`}
          >
            {s.label}
          </button>
        ))}
        {(sub === "mes" || sub === "envios") && (
          <label className="ml-auto flex items-center gap-2 text-sm">
            Mes
            <input type="month" className="input" value={mes} max={thisMonth()} onChange={(e) => e.target.value && setMes(e.target.value)} />
          </label>
        )}
      </div>
      <p className="text-xs text-muted">
        Comisión = base cobrada del mes × % del tabulador (según cumplimiento de meta) × factor de ocupación + ajustes. El resultado aparece en el
        módulo <strong>Comisiones</strong>, tabla «Cálculo de comisiones por vendedor y mes».
        {data.updated_at && ` Plan editado por ${data.updated_by} el ${new Date(data.updated_at).toLocaleString("es-MX")}.`}
      </p>
      {sub === "plan" && <PlanEditor initial={data.plan} onSaved={() => qc.invalidateQueries({ queryKey: ["comis-plan"] })} />}
      {sub === "reglas" && <RulesEditor initial={data.plan} onSaved={() => qc.invalidateQueries({ queryKey: ["comis-plan"] })} />}
      {sub === "mes" && <MonthEditor plan={data.plan} mes={mes} />}
      {sub === "envios" && <Statements mes={mes} />}
    </div>
  );
}
