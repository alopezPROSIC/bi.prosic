import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useFilters } from "../hooks/useFilters";
import { formatCell } from "../lib/format";
import type { CommissionAlert, CommissionAlertLevel, CommissionPanel as Panel, CommissionSeller } from "../types/api";
import { IconAlert, IconCheck, IconCritical, IconInfo, IconUp } from "./Icons";

const money = (v: number | null | undefined) => formatCell(v ?? 0, "money");
const pct = (v: number | null | undefined, d = 2) => (v == null ? "—" : `${v.toLocaleString("es-MX", { maximumFractionDigits: d })} %`);
const date = (v: string | null | undefined) => (v ? formatCell(v, "date") : "—");

const LEVEL: Record<CommissionAlertLevel, { label: string; cls: string; Icon: typeof IconAlert }> = {
  riesgo: { label: "En riesgo", cls: "text-bad bg-bad/10", Icon: IconCritical },
  revisar: { label: "Revisar", cls: "text-warn bg-warn/10", Icon: IconAlert },
  oportunidad: { label: "Oportunidad", cls: "text-good bg-good/10", Icon: IconUp },
  info: { label: "Para saber", cls: "text-ink-2 bg-line/60", Icon: IconInfo },
  ok: { label: "En orden", cls: "text-good bg-good/10", Icon: IconCheck },
};
const ACTION: Record<string, { label: string; to: string }> = {
  reglas: { label: "Ir a Reglas y depuración", to: "/admin?tab=comisiones&sub=reglas" },
  plan: { label: "Ir al plan de comisiones", to: "/admin?tab=comisiones&sub=plan" },
  metas: { label: "Ir a metas del mes", to: "/admin?tab=comisiones&sub=mes" },
};

/** Tramo del tabulador que aplica a un % de cumplimiento (el mayor «desde» alcanzado). */
function tramoOf(tramos: { desde: number; valor: number }[], x: number | null): number | null {
  if (x == null) return null;
  let hit: number | null = null;
  for (const t of [...tramos].sort((a, b) => a.desde - b.desde)) if (x + 1e-9 >= t.desde) hit = t.valor;
  return hit;
}

function Box({ title, hint, children, className = "" }: { title: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card min-w-0 p-4 ${className}`}>
      <h3 className="text-sm font-semibold">{title}</h3>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: ReactNode; tone?: "bad" | "good" }) {
  return (
    <div className="rounded-lg border border-line p-3">
      <p className="text-xs text-ink-2">{label}</p>
      <p className={`text-2xl font-semibold tracking-tight ${tone === "bad" ? "text-bad" : ""}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
    </div>
  );
}

// ------------------------------------------------------------------ qué revisar
function Alerts({ alerts, onSeller, isAdmin }: { alerts: CommissionAlert[]; onSeller: (id: number) => void; isAdmin: boolean }) {
  const [filter, setFilter] = useState<CommissionAlertLevel | "todas">("todas");
  const levels = [...new Set(alerts.map((a) => a.nivel))];
  const shown = filter === "todas" ? alerts : alerts.filter((a) => a.nivel === filter);
  return (
    <Box title="Qué revisar" hint="Se recalcula con cada cambio del plan, las reglas o los filtros.">
      {levels.length > 1 && (
        <div className="mb-3 flex flex-wrap gap-1" role="tablist" aria-label="Tipo de aviso">
          {(["todas", ...levels] as const).map((l) => (
            <button
              key={l}
              role="tab"
              aria-selected={filter === l}
              onClick={() => setFilter(l)}
              className={`rounded-full border px-2.5 py-0.5 text-xs ${filter === l ? "border-brand bg-brand/10 font-medium text-brand" : "border-line text-ink-2"}`}
            >
              {l === "todas" ? `Todos (${alerts.length})` : `${LEVEL[l].label} (${alerts.filter((a) => a.nivel === l).length})`}
            </button>
          ))}
        </div>
      )}
      <ul className="space-y-2">
        {shown.map((a, i) => {
          const { cls, Icon, label } = LEVEL[a.nivel];
          const ids = a.vendedores.filter((x): x is number => x !== null);
          const act = a.accion && ACTION[a.accion];
          return (
            <li key={i} className="rounded-lg border border-line p-3">
              <div className="flex items-start gap-2.5">
                <span className={`mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`}>
                  <Icon width={13} height={13} /> {label}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium">{a.titulo}</p>
                  <p className="mt-0.5 text-xs text-ink-2">{a.detalle}</p>
                  {(ids.length > 0 || (act && isAdmin)) && (
                    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                      {ids.length > 0 && (
                        <button className="text-brand hover:underline" onClick={() => onSeller(ids[0])}>
                          Ver {ids.length > 1 ? "al primero" : "su ficha"}
                        </button>
                      )}
                      {act && isAdmin && <Link className="text-brand hover:underline" to={act.to}>{act.label}</Link>}
                    </div>
                  )}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </Box>
  );
}

// ------------------------------------------------------------------ de lo cobrado a la comisión
function Funnel({ p }: { p: Panel }) {
  const max = Math.max(...p.embudo.map((s) => Math.abs(s.monto)), 1);
  return (
    <Box
      title="De lo cobrado a la comisión"
      hint={`${p.meses.map((m) => m.label).join(", ")} (meses completos del cálculo)${p.preliminar ? " · mes en curso: preliminar" : ""}`}
    >
      <ol className="space-y-2.5">
        {p.embudo.map((s) => {
          const minus = s.tipo === "resta";
          const strong = s.tipo === "resultado" || s.tipo === "total";
          if (minus && !s.monto) return null;
          return (
            <li key={s.key} className={s.tipo === "comision" ? "border-t border-line pt-2.5" : ""}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className={strong || s.tipo === "comision" ? "font-semibold" : "text-ink-2"}>
                  {minus ? "− " : s.tipo === "resultado" ? "= " : ""}
                  {s.label}
                </span>
                <span className={`tabular whitespace-nowrap ${strong || s.tipo === "comision" ? "font-semibold" : ""}`}>{money(Math.abs(s.monto))}</span>
              </div>
              {s.tipo !== "comision" && (
                <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-line/50" aria-hidden>
                  <div
                    className={`h-full rounded-full ${strong ? "bg-brand" : "bg-ink-2/40"}`}
                    style={{ width: `${Math.max(1, (Math.abs(s.monto) / max) * 100)}%` }}
                  />
                </div>
              )}
              {s.detalle && <p className="mt-0.5 text-[11px] text-muted">{s.detalle}</p>}
            </li>
          );
        })}
      </ol>
    </Box>
  );
}

// ------------------------------------------------------------------ ficha del vendedor
function Meter({ s, simBase }: { s: CommissionSeller; simBase: number }) {
  if (!s.meta || !s.tramos.length) return null;
  const top = Math.max(120, ...s.tramos.map((t) => t.desde + 20));
  const now = (s.base_mes / s.meta) * 100;
  const sim = (simBase / s.meta) * 100;
  const x = (v: number) => `${Math.min(100, (v / top) * 100)}%`;
  return (
    <div className="mt-4">
      <div className="flex justify-between text-xs text-ink-2">
        <span>Cumplimiento de la meta ({money(s.meta)})</span>
        <span className="font-medium text-ink">{pct(now, 1)}{sim > now + 0.05 && ` → ${pct(sim, 1)}`}</span>
      </div>
      <div className="relative mt-1.5 h-3 rounded-full bg-line/50">
        {sim > now && <div className="absolute inset-y-0 left-0 rounded-full bg-brand/30" style={{ width: x(sim) }} />}
        <div className="absolute inset-y-0 left-0 rounded-full bg-brand" style={{ width: x(now) }} />
        {s.tramos.map((t) => (
          <span key={t.desde} className="absolute -top-0.5 h-4 w-0.5 bg-ink/60" style={{ left: x(t.desde) }} aria-hidden />
        ))}
      </div>
      <div className="relative mt-1 h-8 text-[10px] text-muted">
        {s.tramos.map((t) => (
          <span key={t.desde} className="absolute -translate-x-1/2 whitespace-nowrap text-center leading-tight" style={{ left: x(t.desde) }}>
            {t.desde} %<br />→ {t.valor} %
          </span>
        ))}
      </div>
    </div>
  );
}

function Simulator({ s, onChange }: { s: CommissionSeller; onChange: (extra: number) => void }) {
  const [extra, setExtra] = useState(0);
  useEffect(() => { setExtra(0); onChange(0); }, [s.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const max = Math.max(s.potencial_base, s.siguiente?.falta_base ?? 0, s.base_mes * 0.5, 10_000);
  const step = max > 1_000_000 ? 10_000 : max > 100_000 ? 1_000 : 100;
  const base = s.base_mes + extra;
  const cump = s.meta ? (base / s.meta) * 100 : null;
  const rate = s.meta && s.tramos.length ? tramoOf(s.tramos, cump) ?? 0 : s.pct_mes || s.pct;
  const adj = s.comision_mes - (s.base_mes * (s.pct_mes || 0)) / 100 * s.factor; // ajustes del mes
  const est = (base * rate) / 100 * s.factor + adj;
  const set = (v: number) => { const n = Math.max(0, Math.min(max, v)); setExtra(n); onChange(n); };
  return (
    <div className="mt-4 rounded-lg border border-brand/30 bg-brand/5 p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-semibold">Simulador: ¿y si cobra más este mes?</p>
        <p className="text-xs text-ink-2">Estimado con el {s.base_tipo === "Sucursal" ? "cobro de su sucursal" : "plan vigente"}</p>
      </div>
      <input
        type="range"
        min={0}
        max={max}
        step={step}
        value={extra}
        onChange={(e) => set(Number(e.target.value))}
        className="mt-3 w-full accent-[rgb(var(--brand))]"
        aria-label="Cobranza adicional"
      />
      <div className="mt-1 flex flex-wrap gap-2 text-xs">
        <button className="rounded-full border border-line bg-surface px-2.5 py-0.5 hover:border-brand" onClick={() => set(0)}>Sin cambio</button>
        {s.siguiente && s.siguiente.falta_base > 0 && (
          <button className="rounded-full border border-line bg-surface px-2.5 py-0.5 hover:border-brand" onClick={() => set(s.siguiente!.falta_base)}>
            Lo que falta para el {s.siguiente.valor} %
          </button>
        )}
        {s.potencial_base > 0 && (
          <button className="rounded-full border border-line bg-surface px-2.5 py-0.5 hover:border-brand" onClick={() => set(s.potencial_base)}>
            Toda su cartera abierta ({money(s.potencial_base)})
          </button>
        )}
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div><dt className="text-xs text-ink-2">Cobranza adicional</dt><dd className="font-semibold tabular">{money(extra)}</dd></div>
        <div><dt className="text-xs text-ink-2">Base del mes</dt><dd className="font-semibold tabular">{money(base)}</dd></div>
        <div><dt className="text-xs text-ink-2">% de comisión</dt><dd className="font-semibold tabular">{pct(rate)}</dd></div>
        <div>
          <dt className="text-xs text-ink-2">Comisión estimada</dt>
          <dd className="font-semibold tabular">
            {money(est)}
            {est - s.comision_mes > 0.5 && <span className="ml-1 text-xs font-medium text-good">+{money(est - s.comision_mes)}</span>}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function InvoiceList({ title, rows, open, maxDias }: { title: string; rows: CommissionSeller["facturas_top"]; open?: boolean; maxDias: number }) {
  return (
    <div className="min-w-0">
      <h4 className="mb-1.5 text-xs font-semibold text-ink-2">{title}</h4>
      {rows.length === 0 ? (
        <p className="text-xs text-muted">Sin facturas.</p>
      ) : (
        <ul className="divide-y divide-line/70 text-sm">
          {rows.map((f) => {
            const risky = open && f.dias != null && f.dias >= maxDias - 60;
            return (
              <li key={f.factura} className="flex items-baseline justify-between gap-2 py-1.5">
                <span className="min-w-0">
                  <span className="block truncate">{f.cliente}</span>
                  <span className="block text-[11px] text-muted">
                    {f.factura} · {open ? `${f.dias ?? "—"} días · cobrado ${pct(f.cobrado_pct, 0)}` : `cobrada ${date(f.fecha_cobro)}`}
                    {f.traspaso && ` · traspasada de ${f.traspaso}`}
                  </span>
                  {risky && (
                    <span className="mt-0.5 inline-flex items-center gap-1 text-[11px] font-medium text-bad">
                      <IconCritical width={11} height={11} /> Comisiona solo si se cobra antes del {date(f.limite)}
                    </span>
                  )}
                </span>
                <span className="shrink-0 tabular">{money(f.base)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function SellerCard({ s, maxDias }: { s: CommissionSeller; maxDias: number }) {
  const [extra, setExtra] = useState(0);
  return (
    <section className="card min-w-0 p-4" aria-label={`Ficha de ${s.nombre}`}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold">{s.nombre}</h3>
          <p className="text-xs text-ink-2">
            {s.puesto} · {s.sucursal} · comisiona sobre {s.base_tipo === "Sucursal" ? "toda su sucursal" : "su cobranza"}
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {!s.en_plan && <span className="rounded-full bg-warn/10 px-2 py-0.5 text-[11px] font-medium text-warn">Fuera del plan</span>}
          {s.en_plan && !s.activo && <span className="rounded-full bg-warn/10 px-2 py-0.5 text-[11px] font-medium text-warn">Inactivo</span>}
        </div>
      </header>
      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile label="Comisión del periodo" value={money(s.comision)} sub={s.ajustes ? `incluye ajustes por ${money(s.ajustes)}` : undefined} />
        <Tile label="Base cobrada" value={money(s.base)} sub={`${s.facturas} facturas`} />
        <Tile label="% de comisión" value={pct(s.pct_mes || s.pct)} sub={s.factor !== 1 ? `factor de ocupación ${s.factor}` : undefined} />
        <Tile
          label="Potencial en cartera"
          value={money(s.potencial_comision)}
          sub={`${s.abiertas} facturas por ${money(s.potencial_base)}`}
        />
      </div>
      {s.meses.length > 1 && (
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {s.meses.map((m) => (
            <span key={m.mes} className="rounded-md border border-line px-2 py-1">
              {m.label}: <b className="tabular">{money(m.comision)}</b>
            </span>
          ))}
        </div>
      )}
      <Meter s={s} simBase={s.base_mes + extra} />
      {s.en_plan && s.activo && <Simulator s={s} onChange={setExtra} />}
      {s.observaciones.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-xs text-ink-2">
          {s.observaciones.map((o) => (
            <li key={o} className="flex items-start gap-1.5"><IconInfo width={12} height={12} className="mt-0.5 shrink-0" /> {o}</li>
          ))}
        </ul>
      )}
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <InvoiceList title="Facturas que más comisionan" rows={s.facturas_top} maxDias={maxDias} />
        <InvoiceList title="Cartera abierta que comisionaría al cobrarse" rows={s.abiertas_top} open maxDias={maxDias} />
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ panel
type Sort = "comision" | "potencial_comision" | "riesgo_comision";

export function CommissionPanel() {
  const { filters } = useFilters();
  const { user } = useAuth();
  const { data, isLoading, error } = useQuery({
    queryKey: ["comis-panel", filters],
    queryFn: () => api.comisiones.panel(filters),
    staleTime: 5 * 60_000,
    placeholderData: (prev) => prev,
  });
  const [selected, setSelected] = useState<number | null | undefined>(undefined);
  const [sort, setSort] = useState<Sort>("comision");
  const [text, setText] = useState("");
  const card = useRef<HTMLDivElement>(null);

  const sellers = useMemo(() => {
    const t = text.trim().toLowerCase();
    return [...(data?.vendedores ?? [])]
      .filter((s) => !t || s.nombre.toLowerCase().includes(t) || s.sucursal.toLowerCase().includes(t))
      .sort((a, b) => b[sort] - a[sort] || a.nombre.localeCompare(b.nombre));
  }, [data, sort, text]);

  if (error) return <p role="alert" className="text-sm text-bad">Panel de comisiones: {(error as Error).message}</p>;
  if (isLoading || !data) return <div className="skeleton h-56 w-full" />;
  const r = data.resumen;
  const current = data.vendedores.find((s) => s.id === selected) ?? sellers[0];
  const pick = (id: number) => {
    setSelected(id);
    setText("");
    setTimeout(() => card.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };
  const maxSort = Math.max(...sellers.map((s) => s[sort]), 1);

  return (
    <div className="space-y-4 print:hidden">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Comisión calculada" value={money(r.comision)} sub={`${r.vendedores} vendedores · ${data.meses.map((m) => m.label).join(", ")}`} />
        <Tile
          label="Base comisionable"
          value={money(r.base_comisionable)}
          sub={data.cuentas.length ? `Solo ${data.cuentas.join(", ")}` : "Todas las cuentas de comisión"}
        />
        <Tile
          label="Comisión potencial en cartera"
          value={r.cartera_disponible ? money(r.potencial_comision) : "—"}
          sub={r.cartera_disponible ? `Si se cobran ${money(r.potencial_base)} de facturas abiertas` : "Odoo no respondió; se reintenta al recargar"}
        />
        <Tile
          label="Comisión en riesgo"
          value={money(r.riesgo_comision)}
          tone={r.riesgo_comision > 0 ? "bad" : undefined}
          sub={`Facturas por ${money(r.riesgo_base)} cumplen ${data.max_dias} días en menos de 60`}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <Alerts alerts={data.alertas} onSeller={pick} isAdmin={Boolean(user?.is_admin)} />
        <Funnel p={data} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[18rem_minmax(0,1fr)]" ref={card}>
        <section className="card h-fit min-w-0 p-3">
          <h3 className="px-1 text-sm font-semibold">Vendedores</h3>
          <div className="mt-2 flex gap-2">
            <input className="input min-w-0 flex-1" placeholder="Buscar…" value={text} onChange={(e) => setText(e.target.value)} aria-label="Buscar vendedor" />
            <select className="input" value={sort} onChange={(e) => setSort(e.target.value as Sort)} aria-label="Ordenar por">
              <option value="comision">Comisión</option>
              <option value="potencial_comision">Potencial</option>
              <option value="riesgo_comision">En riesgo</option>
            </select>
          </div>
          <ul className="mt-2 max-h-[34rem] space-y-0.5 overflow-y-auto" aria-label="Vendedores">
            {sellers.map((s) => {
              const active = current?.id === s.id;
              return (
                <li key={String(s.id)}>
                  <button
                    className={`w-full rounded-lg px-2 py-1.5 text-left ${active ? "bg-brand/10" : "hover:bg-line/50"}`}
                    onClick={() => setSelected(s.id)}
                    aria-current={active}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className={`truncate text-sm ${active ? "font-semibold text-brand" : ""}`}>{s.nombre}</span>
                      <span className="shrink-0 text-xs tabular">{money(s[sort])}</span>
                    </div>
                    <div className="mt-1 h-1 rounded-full bg-line/50" aria-hidden>
                      <div className="h-full rounded-full bg-brand/70" style={{ width: `${(s[sort] / maxSort) * 100}%` }} />
                    </div>
                    <div className="mt-0.5 flex gap-2 text-[11px] text-muted">
                      <span className="truncate">{s.puesto} · {s.sucursal}</span>
                      {(!s.en_plan || s.riesgo_base > 0 || s.observaciones.some((o) => o.startsWith("Falta"))) && (
                        <span className="shrink-0 text-warn">● revisar</span>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
            {sellers.length === 0 && <li className="px-2 py-3 text-xs text-muted">Sin coincidencias.</li>}
          </ul>
        </section>
        {current ? <SellerCard s={current} maxDias={data.max_dias} /> : <div className="card p-6 text-sm text-muted">Sin vendedores en el periodo.</div>}
      </div>
    </div>
  );
}
