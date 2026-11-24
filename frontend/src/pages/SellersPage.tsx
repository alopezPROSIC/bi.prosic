import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { IconDown, IconPrint, IconRefresh, IconUp } from "../components/Icons";
import { ModuleView } from "../components/ModuleView";
import { CardsSkeleton, ChartSkeleton, ErrorState } from "../components/States";
import { useAuth } from "../hooks/useAuth";
import { useFilters } from "../hooks/useFilters";
import { formatDate, formatValue } from "../lib/format";
import { canSee } from "../lib/permissions";
import type { SellerCommission, SellerRow } from "../types/api";

const money = (v: number | null | undefined, compact = true) => formatValue(v ?? null, "MXN", compact);
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v.toLocaleString("es-MX", { maximumFractionDigits: 1 })} %`);

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0])
    .join("")
    .toUpperCase();
}

function Avatar({ name, size = "sm" }: { name: string; size?: "sm" | "lg" }) {
  const cls = size === "lg" ? "h-14 w-14 text-lg" : "h-8 w-8 text-xs";
  return (
    <span className={`inline-flex shrink-0 items-center justify-center rounded-full bg-accent font-bold text-accent-ink ${cls}`} aria-hidden>
      {initials(name)}
    </span>
  );
}

function Delta({ cur, prev }: { cur: number; prev: number | null | undefined }) {
  if (prev === null || prev === undefined || prev === 0) return <span className="text-muted">—</span>;
  const d = ((cur - prev) / Math.abs(prev)) * 100;
  const up = d >= 0;
  return (
    <span className={`inline-flex items-center gap-0.5 font-medium ${up ? "text-good" : "text-bad"}`} title={`Antes: ${money(prev, false)}`}>
      {up ? <IconUp /> : <IconDown />}
      {up ? "+" : ""}
      {d.toLocaleString("es-MX", { maximumFractionDigits: 1 })}%
    </span>
  );
}

function RankMove({ r }: { r: SellerRow }) {
  if (r.rank_anterior === null || r.rank_anterior === undefined) return <span className="text-xs text-muted">nuevo</span>;
  const diff = r.rank_anterior - r.rank;
  if (diff === 0) return <span className="text-xs text-muted">=</span>;
  return (
    <span className={`text-xs font-medium ${diff > 0 ? "text-good" : "text-bad"}`} title={`Antes: #${r.rank_anterior}`}>
      {diff > 0 ? "▲" : "▼"} {Math.abs(diff)}
    </span>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card min-w-0 p-4">
      <div className="text-xs font-medium text-ink-2">{label}</div>
      <div className="mt-1 truncate text-2xl font-semibold tracking-tight">{value}</div>
      {hint && <div className="mt-0.5 truncate text-xs text-muted" title={hint}>{hint}</div>}
    </div>
  );
}

function periodText(p: { date_from: string; date_to: string } | null | undefined) {
  return p ? `${formatDate(p.date_from)} – ${formatDate(p.date_to)}` : "";
}

// ------------------------------------------------------------------ ranking
export function SellersPage() {
  const { filters } = useFilters();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const iva = filters.iva === "con" ? "con IVA" : "sin IVA";
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["vendedores", filters],
    queryFn: () => api.vendedores.ranking(filters),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });

  const rows = useMemo(() => {
    const s = q.trim().toLowerCase();
    return (data?.vendedores ?? []).filter((r) => r.facturado || r.pedidos_monto).filter((r) => !s || r.nombre.toLowerCase().includes(s));
  }, [data, q]);
  const active = (data?.vendedores ?? []).filter((r) => r.facturado > 0);
  const top3 = active.slice(0, 3).reduce((a, r) => a + r.facturado, 0);
  const max = Math.max(1, ...rows.map((r) => r.facturado));
  const leader = active[0];
  const prevTotal = (data?.vendedores ?? []).reduce((a, r) => a + (r.facturado_anterior ?? 0), 0);

  const refresh = async () => {
    setRefreshing(true);
    try {
      qc.setQueryData(["vendedores", filters], await api.vendedores.ranking(filters, true));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Vendedores</h1>
          <p className="text-xs text-muted">
            Facturado neto ({iva}, sin anticipos, menos notas de crédito) · {periodText(data?.periodo)}
            {data?.anterior && ` · vs ${periodText(data.anterior)}`}
          </p>
        </div>
        <button className="btn-ghost text-xs print:hidden" onClick={refresh} disabled={refreshing || isFetching} title="Consultar Odoo sin caché">
          <IconRefresh width={14} height={14} className={refreshing || isFetching ? "animate-spin" : ""} /> Actualizar
        </button>
      </div>

      {isLoading ? (
        <CardsSkeleton n={4} />
      ) : isError ? (
        <ErrorState message={(error as Error).message} onRetry={() => refetch()} />
      ) : data ? (
        <div className={`space-y-4 ${isFetching ? "opacity-60 transition-opacity" : ""}`}>
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <Stat label={`Facturado del equipo (${iva})`} value={money(data.total)} hint={prevTotal ? `Antes: ${money(prevTotal)}` : undefined} />
            <Stat label="Vendedores con facturación" value={String(active.length)} />
            <Stat
              label="Concentración: top 3"
              value={pct(data.total ? (top3 / data.total) * 100 : null)}
              hint={active.slice(0, 3).map((r) => r.nombre.split(" ")[0]).join(", ")}
            />
            <Stat label="Líder del periodo" value={leader ? leader.nombre.split(" ").slice(0, 2).join(" ") : "—"} hint={leader ? `${money(leader.facturado)} · ${pct(leader.participacion)}` : undefined} />
          </div>

          <section className="card min-w-0 p-4">
            <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">Ranking por facturado — clic en un vendedor para ver su ficha</h2>
              <input className="input w-full sm:w-64" placeholder="Buscar vendedor…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Buscar vendedor" />
            </header>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-ink-2">
                    <th className="w-12 py-2 pr-2 font-medium">#</th>
                    <th className="py-2 pr-2 font-medium">Vendedor</th>
                    <th className="py-2 pr-2 text-right font-medium">Facturado</th>
                    <th className="hidden py-2 pr-2 font-medium md:table-cell">Participación</th>
                    <th className="py-2 pr-2 text-right font-medium">vs anterior</th>
                    <th className="hidden py-2 pr-2 text-right font-medium lg:table-cell">Pedidos</th>
                    <th className="hidden py-2 text-right font-medium lg:table-cell">Facturas</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={r.id ?? r.nombre}
                      className={`border-b border-line/60 ${r.id ? "cursor-pointer hover:bg-line/30" : ""}`}
                      onClick={() => r.id && navigate(`/vendedores/${r.id}`)}
                    >
                      <td className="py-2 pr-2">
                        <div className="flex flex-col leading-tight">
                          <span className="tabular font-semibold">{r.rank}</span>
                          <RankMove r={r} />
                        </div>
                      </td>
                      <td className="py-2 pr-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <Avatar name={r.nombre} />
                          {r.id ? (
                            <Link to={`/vendedores/${r.id}`} className="truncate font-medium hover:underline" onClick={(e) => e.stopPropagation()}>
                              {r.nombre}
                            </Link>
                          ) : (
                            <span className="truncate text-muted">{r.nombre}</span>
                          )}
                        </div>
                      </td>
                      <td className="tabular py-2 pr-2 text-right font-medium">{money(r.facturado, false)}</td>
                      <td className="hidden py-2 pr-2 md:table-cell">
                        <div className="flex items-center gap-2">
                          <div className="h-2 w-28 overflow-hidden rounded-full bg-line" aria-hidden>
                            <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(0, (r.facturado / max) * 100)}%` }} />
                          </div>
                          <span className="tabular text-xs text-ink-2">{pct(r.participacion)}</span>
                        </div>
                      </td>
                      <td className="py-2 pr-2 text-right text-xs">
                        <Delta cur={r.facturado} prev={r.facturado_anterior} />
                      </td>
                      <td className="tabular hidden py-2 pr-2 text-right text-ink-2 lg:table-cell" title={`${r.pedidos} pedidos`}>
                        {money(r.pedidos_monto)}
                      </td>
                      <td className="tabular hidden py-2 text-right text-ink-2 lg:table-cell">{r.facturas.toLocaleString("es-MX")}</td>
                    </tr>
                  ))}
                  {!rows.length && (
                    <tr>
                      <td colSpan={7} className="py-10 text-center text-sm text-muted">Sin vendedores para los filtros seleccionados.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------ ficha
function CommissionStrip({ c }: { c: SellerCommission }) {
  const done = Math.min(100, c.cumplimiento ?? 0);
  return (
    <section className="card p-4">
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">
          Comisión {c.meses?.length ? `· ${c.meses.map((m) => m.label).join(", ")}` : ""} <span className="font-normal text-muted">· sin IVA, sobre la renta cobrada</span>
        </h2>
        <Link to="/comisiones" className="text-xs font-medium text-accent hover:underline">Ver el módulo de comisiones →</Link>
      </header>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <div>
          <div className="text-xs text-ink-2">Comisión calculada{c.preliminar ? " (preliminar)" : ""}</div>
          <div className="text-2xl font-semibold tracking-tight">{money(c.comision, false)}</div>
          <div className="text-xs text-muted">{c.pct !== null ? `${c.pct} % · ${c.puesto ?? ""}` : c.puesto ?? ""}</div>
        </div>
        <div>
          <div className="text-xs text-ink-2">Base comisionable</div>
          <div className="text-2xl font-semibold tracking-tight">{money(c.base)}</div>
          <div className="text-xs text-muted">{c.facturas} facturas cobradas al 100 %</div>
        </div>
        <div>
          <div className="text-xs text-ink-2">Meta del mes</div>
          {c.meta ? (
            <>
              <div className="text-2xl font-semibold tracking-tight">{pct(c.cumplimiento)}</div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-line" aria-hidden>
                <div className="h-full rounded-full bg-accent" style={{ width: `${done}%` }} />
              </div>
              <div className="mt-0.5 text-xs text-muted">de {money(c.meta)}</div>
            </>
          ) : (
            <div className="mt-1 text-sm text-muted">Sin meta capturada</div>
          )}
        </div>
        <div>
          <div className="text-xs text-ink-2">Cartera abierta</div>
          <div className="text-2xl font-semibold tracking-tight">{money(c.potencial_comision, false)}</div>
          <div className="text-xs text-muted">
            potencial sobre {money(c.potencial_base)}
            {c.riesgo_comision > 0 && <span className="text-bad"> · {money(c.riesgo_comision, false)} en riesgo</span>}
          </div>
        </div>
      </div>
      {c.siguiente && (
        <p className="mt-3 rounded-lg bg-line/40 px-3 py-2 text-xs text-ink-2">
          Le faltan <b>{money(c.siguiente.falta_base)}</b> de base cobrada para el siguiente tramo ({c.siguiente.valor} %): serían{" "}
          <b>{money(c.siguiente.comision_extra, false)}</b> más.
        </p>
      )}
    </section>
  );
}

export function SellerPage() {
  const { id } = useParams();
  const sellerId = Number(id);
  const { filters } = useFilters();
  const { user } = useAuth();
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);
  const iva = filters.iva === "con" ? "con IVA" : "sin IVA";
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["vendedor", sellerId, filters],
    queryFn: () => api.vendedores.profile(sellerId, filters),
    enabled: Number.isFinite(sellerId),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
  });
  const r = data?.ranking;

  const refresh = async () => {
    setRefreshing(true);
    try {
      qc.setQueryData(["vendedor", sellerId, filters], await api.vendedores.profile(sellerId, filters, true));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2 print:hidden">
        <Link to="/vendedores" className="text-xs font-medium text-ink-2 hover:text-ink">← Todos los vendedores</Link>
        <div className="flex gap-1">
          <button className="btn-ghost text-xs" onClick={() => window.print()} disabled={!data} title="Imprimir o guardar como PDF">
            <IconPrint width={14} height={14} /> PDF
          </button>
          <button className="btn-ghost text-xs" onClick={refresh} disabled={refreshing || isFetching} title="Consultar Odoo sin caché">
            <IconRefresh width={14} height={14} className={refreshing || isFetching ? "animate-spin" : ""} /> Actualizar
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-4">
          <div className="skeleton h-28 w-full" />
          <CardsSkeleton />
          <div className="grid gap-4 xl:grid-cols-2">
            <ChartSkeleton />
            <ChartSkeleton />
          </div>
        </div>
      ) : isError ? (
        <ErrorState message={(error as Error).message} onRetry={() => refetch()} />
      ) : data ? (
        <div className={`space-y-4 ${isFetching ? "opacity-60 transition-opacity" : ""}`}>
          <section className="card flex flex-wrap items-center gap-4 p-5">
            <Avatar name={data.nombre} size="lg" />
            <div className="min-w-0 flex-1">
              <h1 className="truncate text-xl font-semibold tracking-tight">{data.nombre}</h1>
              <p className="text-sm text-ink-2">
                {r ? (
                  <>
                    Posición <b>#{r.rank}</b> de {data.vendedores} · <b>{pct(r.participacion)}</b> del facturado de la empresa
                  </>
                ) : (
                  "Sin facturación en el periodo"
                )}
              </p>
              <p className="text-xs text-muted">
                {periodText(data.kpis.period)}
                {data.kpis.previous_period && ` · vs ${periodText(data.kpis.previous_period)}`} · importes {iva}
              </p>
            </div>
            {r && (
              <div className="w-full border-t border-line pt-3 sm:w-auto sm:border-0 sm:pt-0 sm:text-right">
                <div className="text-xs text-ink-2">Facturado neto</div>
                <div className="text-3xl font-semibold tracking-tight">{money(r.facturado)}</div>
                <div className="text-xs">
                  <Delta cur={r.facturado} prev={r.facturado_anterior} />
                  {r.rank_anterior != null && r.rank_anterior !== r.rank && <span className="ml-2 text-muted">antes #{r.rank_anterior}</span>}
                </div>
              </div>
            )}
          </section>

          {data.comision && canSee(user, "comisiones") && <CommissionStrip c={data.comision} />}

          <ModuleView data={data.kpis} />
        </div>
      ) : null}
    </div>
  );
}
