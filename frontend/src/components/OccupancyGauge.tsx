import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import { useChartColors } from "../hooks/useChartColors";
import type { OccupancyReading, OccupancyResponse } from "../types/api";
import { IconAlert, IconCheck, IconCopy, IconCritical, IconDownload, IconRefresh } from "./Icons";
import { ErrorState } from "./States";

// Tacómetro de ocupación diaria de la flota (reemplaza la imagen que Contabilidad enviaba mañana y noche).
// Misma definición que Rentas e Histórico: unidades en estado ocupado / unidades de la flota de renta.

type Slot = "manana" | "ahora" | "cierre";
type Status = "good" | "warn" | "bad";

const STATUS_TEXT: Record<Status, string> = { good: "En meta", warn: "Debajo de la meta", bad: "Debajo del mínimo" };
const STATUS_CLASS: Record<Status, string> = { good: "bg-good/10 text-good", warn: "bg-warn/10 text-warn", bad: "bg-bad/10 text-bad" };
const STATUS_HEX: Record<Status, string> = { good: "#13804c", warn: "#9a6a00", bad: "#b42318" };

export function statusOf(pct: number | null | undefined, meta: number, minimo: number): Status {
  if (pct == null) return "warn";
  return pct >= meta ? "good" : pct >= minimo ? "warn" : "bad";
}

export function StatusPill({ status, className = "" }: { status: Status; className?: string }) {
  const Icon = status === "good" ? IconCheck : status === "warn" ? IconAlert : IconCritical;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${STATUS_CLASS[status]} ${className}`}>
      <Icon width={14} height={14} />
      {STATUS_TEXT[status]}
    </span>
  );
}

const pctText = (v: number | null | undefined) => (v == null ? "—" : `${v.toLocaleString("es-MX", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`);
const intText = (v: number) => v.toLocaleString("es-MX");
const timeText = (ts: string) => new Date(ts).toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" });
const dateText = (ts: string) => new Date(ts).toLocaleDateString("es-MX", { weekday: "long", day: "numeric", month: "long" });
const dayText = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString("es-MX", { day: "numeric", month: "short" });

// ------------------------------------------------------------------ geometría del tacómetro (semicírculo 0–100 %)
const CX = 120;
const CY = 118;
const R = 96;
function pt(p: number, r = R) {
  const a = Math.PI * (1 - Math.min(100, Math.max(0, p)) / 100);
  return { x: CX + r * Math.cos(a), y: CY - r * Math.sin(a) };
}
function arc(p0: number, p1: number, r = R) {
  const a = pt(p0, r);
  const b = pt(p1, r);
  return `M ${a.x.toFixed(2)} ${a.y.toFixed(2)} A ${r} ${r} 0 0 1 ${b.x.toFixed(2)} ${b.y.toFixed(2)}`;
}

export function Gauge({ pct, meta, minimo, size = "lg" }: { pct: number | null; meta: number; minimo: number; size?: "lg" | "sm" }) {
  const status = statusOf(pct, meta, minimo);
  const v = pct ?? 0;
  const angle = -90 + (Math.min(100, Math.max(0, v)) / 100) * 180;
  const marks = [
    { p: minimo, label: `Mín. ${minimo}%`, cls: "bad" },
    { p: meta, label: `Meta ${meta}%`, cls: "good" },
  ];
  return (
    <svg
      viewBox="0 0 240 142"
      className={size === "lg" ? "w-full max-w-[360px]" : "w-full max-w-[180px]"}
      role="img"
      aria-label={`Ocupación ${pctText(pct)}; ${STATUS_TEXT[status]} (mínimo ${minimo} %, meta ${meta} %)`}
    >
      {/* pista: zonas en un paso claro de su propio color */}
      <path d={arc(0, minimo)} fill="none" strokeWidth={14} style={{ stroke: "rgb(var(--bad) / 0.14)" }} />
      <path d={arc(minimo, meta)} fill="none" strokeWidth={14} style={{ stroke: "rgb(var(--warn) / 0.16)" }} />
      <path d={arc(meta, 100)} fill="none" strokeWidth={14} style={{ stroke: "rgb(var(--good) / 0.14)" }} />
      {/* relleno: el color lo da el estado */}
      {pct != null && pct > 0 && (
        <path d={arc(0, v)} fill="none" strokeWidth={14} strokeLinecap="round" style={{ stroke: `rgb(var(--${status}))`, transition: "d 600ms ease" }} />
      )}
      {/* marcas de 0, 25, 50, 75, 100 */}
      {[25, 50, 75].map((p) => {
        const a = pt(p, R - 12);
        const b = pt(p, R - 18);
        const t = pt(p, R - 29);
        return (
          <g key={p}>
            <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={1.2} style={{ stroke: "rgb(var(--muted))" }} />
            {size === "lg" && (
              <text x={t.x} y={t.y + 3} textAnchor="middle" fontSize={9} style={{ fill: "rgb(var(--muted))" }}>
                {p}
              </text>
            )}
          </g>
        );
      })}
      {/* extremos: 0 % y 100 % debajo de cada punta del arco */}
      {size === "lg" &&
        [0, 100].map((p) => (
          <text key={p} x={pt(p).x} y={CY + 17} textAnchor="middle" fontSize={9} style={{ fill: "rgb(var(--muted))" }}>
            {p}%
          </text>
        ))}
      {/* mínimo y meta */}
      {marks.map((m) => {
        const a = pt(m.p, R + 9);
        const b = pt(m.p, R - 9);
        const t = pt(m.p, R + 17);
        return (
          <g key={m.label}>
            <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={2} strokeLinecap="round" style={{ stroke: `rgb(var(--${m.cls}))` }} />
            {size === "lg" && (
              <text x={t.x} y={t.y + 3} textAnchor={m.p < 45 ? "end" : m.p > 55 ? "start" : "middle"} fontSize={8.5} fontWeight={600} style={{ fill: "rgb(var(--ink-2))" }}>
                {m.label}
              </text>
            )}
          </g>
        );
      })}
      {/* aguja */}
      {pct != null && (
        <g style={{ transform: `rotate(${angle}deg)`, transformOrigin: `${CX}px ${CY}px`, transition: "transform 700ms cubic-bezier(.2,.8,.2,1)" }}>
          <path d={`M ${CX - 3.5} ${CY} L ${CX} ${CY - R + 22} L ${CX + 3.5} ${CY} Z`} style={{ fill: "rgb(var(--ink))" }} />
        </g>
      )}
      <circle cx={CX} cy={CY} r={7} style={{ fill: "rgb(var(--ink))" }} />
      <circle cx={CX} cy={CY} r={2.5} style={{ fill: "rgb(var(--surface))" }} />
    </svg>
  );
}

// ------------------------------------------------------------------ imagen para compartir (WhatsApp / correo)
function esc(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function shareSvg(d: OccupancyResponse, r: OccupancyReading, label: string, scope: string): string {
  const st = statusOf(r.pct, d.meta, d.minimo);
  const W = 1080;
  const H = 1080;
  const cx = 540;
  const cy = 560;
  const rr = 330;
  const p2 = (p: number, rad = rr) => {
    const a = Math.PI * (1 - Math.min(100, Math.max(0, p)) / 100);
    return [cx + rad * Math.cos(a), cy - rad * Math.sin(a)];
  };
  const ar = (p0: number, p1: number) => {
    const [x0, y0] = p2(p0);
    const [x1, y1] = p2(p1);
    return `M ${x0} ${y0} A ${rr} ${rr} 0 0 1 ${x1} ${y1}`;
  };
  const [nx, ny] = p2(r.pct ?? 0, rr - 70);
  const font = "Figtree, 'Segoe UI', Arial, sans-serif";
  const others: [string, number | null | undefined][] = [
    [`Mañana ${d.hora_manana}`, d.manana?.pct],
    [`Cierre ${d.cierre ? dayText(d.cierre.dia) : ""} ${d.hora_cierre}`, d.cierre?.pct],
    ["Promedio del mes", d.promedio_mes],
  ];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
  <rect width="${W}" height="${H}" fill="#fafbf6"/>
  <rect width="${W}" height="150" fill="#154046"/>
  <rect y="150" width="${W}" height="10" fill="#33e989"/>
  <text x="60" y="78" font-family="${font}" font-size="44" font-weight="700" fill="#f4fff3">PROSIC · Ocupación de flota</text>
  <text x="60" y="122" font-family="${font}" font-size="28" fill="#ebeedc">${esc(dateText(r.ts))} · ${esc(label)} · ${esc(scope)}</text>
  <path d="${ar(0, d.minimo)}" fill="none" stroke="#b42318" stroke-opacity=".15" stroke-width="46"/>
  <path d="${ar(d.minimo, d.meta)}" fill="none" stroke="#9a6a00" stroke-opacity=".17" stroke-width="46"/>
  <path d="${ar(d.meta, 100)}" fill="none" stroke="#13804c" stroke-opacity=".15" stroke-width="46"/>
  ${r.pct ? `<path d="${ar(0, r.pct)}" fill="none" stroke="${STATUS_HEX[st]}" stroke-width="46" stroke-linecap="round"/>` : ""}
  ${[d.minimo, d.meta]
    .map((p, i) => {
      const [x0, y0] = p2(p, rr + 34);
      const [x1, y1] = p2(p, rr - 34);
      const [tx, ty] = p2(p, rr + 62);
      return `<line x1="${x0}" y1="${y0}" x2="${x1}" y2="${y1}" stroke="${i ? "#13804c" : "#b42318"}" stroke-width="6" stroke-linecap="round"/>
  <text x="${tx}" y="${ty + 8}" text-anchor="${p < 45 ? "end" : p > 55 ? "start" : "middle"}" font-family="${font}" font-size="26" font-weight="600" fill="#3f6267">${i ? "Meta" : "Mín."} ${p}%</text>`;
    })
    .join("")}
  <line x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}" stroke="#154046" stroke-width="12" stroke-linecap="round"/>
  <circle cx="${cx}" cy="${cy}" r="24" fill="#154046"/>
  <text x="${cx}" y="${cy + 120}" text-anchor="middle" font-family="${font}" font-size="120" font-weight="700" fill="#154046">${esc(pctText(r.pct))}</text>
  <text x="${cx}" y="${cy + 172}" text-anchor="middle" font-family="${font}" font-size="32" fill="#3f6267">${intText(r.ocupadas)} de ${intText(r.flota)} unidades ocupadas · ${STATUS_TEXT[st]}</text>
  ${others
    .map(
      ([l, v], i) => `<g transform="translate(${60 + i * 330} 850)"><rect width="300" height="150" rx="20" fill="#ffffff" stroke="#e1e5d6" stroke-width="2"/>
  <text x="26" y="52" font-family="${font}" font-size="24" fill="#5f777a">${esc(l)}</text>
  <text x="26" y="112" font-family="${font}" font-size="52" font-weight="700" fill="#154046">${esc(pctText(v))}</text></g>`,
    )
    .join("")}
  <text x="60" y="1050" font-family="${font}" font-size="22" fill="#5f777a">Ocupadas = ${esc(d.estados_ocupados.join(" + "))} · Fuente: Odoo › Flota · PROSIC BI</text>
</svg>`;
}

async function sharePng(svg: string): Promise<Blob> {
  // data: (no blob:): la política de seguridad del sitio solo permite imágenes propias o data:
  const img = new Image();
  await new Promise<void>((ok, fail) => {
    img.onload = () => ok();
    img.onerror = () => fail(new Error("No se pudo generar la imagen"));
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
  const canvas = document.createElement("canvas");
  canvas.width = img.width;
  canvas.height = img.height;
  canvas.getContext("2d")!.drawImage(img, 0, 0);
  return await new Promise<Blob>((ok, fail) => canvas.toBlob((b) => (b ? ok(b) : fail(new Error("No se pudo generar la imagen"))), "image/png"));
}

// ------------------------------------------------------------------ hook de datos
export function useOccupancy(warehouseId: number | null) {
  return useQuery({
    queryKey: ["ocupacion", warehouseId],
    queryFn: () => api.occupancy(warehouseId),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000, // la lectura «ahora» se renueva sola en el servidor cada N minutos
  });
}

function readingOf(d: OccupancyResponse, slot: Slot): OccupancyReading | null {
  return slot === "manana" ? d.manana : slot === "cierre" ? d.cierre : d.ahora;
}

function slotLabel(d: OccupancyResponse, slot: Slot): string {
  if (slot === "manana") return `Corte de la mañana (${d.hora_manana})`;
  if (slot === "cierre") return d.cierre ? `Cierre del ${dayText(d.cierre.dia)} (${d.hora_cierre})` : "Cierre de la noche";
  return d.ahora?.stale ? `Última lectura (${timeText(d.ahora.ts)})` : d.ahora ? `Ahora (leído a las ${timeText(d.ahora.ts)})` : "Ahora";
}

// ------------------------------------------------------------------ tarjeta compacta (Rentas e inventario)
export function OccupancyCompact() {
  const { data, isLoading } = useOccupancy(null);
  if (isLoading) return <div className="skeleton h-32 w-full" />;
  if (!data) return null;
  const r = data.ahora ?? data.cierre;
  const st = statusOf(r?.pct, data.meta, data.minimo);
  return (
    <Link to="/ocupacion" className="card flex flex-wrap items-center gap-4 p-4 transition-colors hover:border-brand/50">
      <div className="w-36 shrink-0">
        <Gauge pct={r?.pct ?? null} meta={data.meta} minimo={data.minimo} size="sm" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-xs text-ink-2">Ocupación de la flota · {data.ahora ? slotLabel(data, "ahora") : slotLabel(data, "cierre")}</p>
        <p className="text-3xl font-semibold tracking-tight">{pctText(r?.pct)}</p>
        <p className="text-xs text-muted">{r ? `${intText(r.ocupadas)} de ${intText(r.flota)} unidades` : ""}</p>
      </div>
      <dl className="grid grid-cols-3 gap-4 text-sm">
        <div>
          <dt className="text-xs text-muted">Mañana</dt>
          <dd className="font-medium">{pctText(data.manana?.pct)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Cierre</dt>
          <dd className="font-medium">{pctText(data.cierre?.pct)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Mes</dt>
          <dd className="font-medium">{pctText(data.promedio_mes)}</dd>
        </div>
      </dl>
      <div className="flex items-center gap-3">
        <StatusPill status={st} />
        <span className="text-xs font-medium text-brand">Ver tacómetro →</span>
      </div>
    </Link>
  );
}

// ------------------------------------------------------------------ vista completa
export function OccupancyBoard({ warehouseId, scopeLabel, onRefresh }: { warehouseId: number | null; scopeLabel: string; onRefresh?: () => void }) {
  const { data, isLoading, error, refetch, isFetching } = useOccupancy(warehouseId);
  const colors = useChartColors();
  const [slot, setSlot] = useState<Slot>("ahora");
  const [msg, setMsg] = useState<string | null>(null);
  const trend = useMemo(() => (data?.tendencia ?? []).map((t) => ({ ...t, label: dayText(t.dia) })), [data]);

  if (error) return <ErrorState message={(error as Error).message} onRetry={() => refetch()} />;
  if (isLoading || !data) return <div className="skeleton h-96 w-full" />;
  const active: Slot = readingOf(data, slot) ? slot : data.ahora ? "ahora" : "cierre";
  const r = readingOf(data, active);
  const st = statusOf(r?.pct, data.meta, data.minimo);
  // diferencia de cada lectura contra la que muestra el tacómetro, en puntos porcentuales
  const compare = (v: number | null | undefined) => (r?.pct == null || v == null ? null : Math.round((v - r.pct) * 10) / 10);
  const tiles: { key: Slot | "mes"; label: string; value: number | null | undefined; sub: string }[] = [
    { key: "manana", label: `Mañana · ${data.hora_manana}`, value: data.manana?.pct, sub: data.manana ? `${intText(data.manana.ocupadas)} de ${intText(data.manana.flota)}` : "Aún sin corte hoy" },
    { key: "ahora", label: "Ahora", value: data.ahora?.pct, sub: data.ahora ? `${data.ahora.stale ? "Última lectura" : "Leído"} ${timeText(data.ahora.ts)}` : "Sin lectura" },
    { key: "cierre", label: `Cierre · ${data.hora_cierre}`, value: data.cierre?.pct, sub: data.cierre ? `Noche del ${dayText(data.cierre.dia)}` : "Sin cierre" },
    { key: "mes", label: "Promedio del mes", value: data.promedio_mes, sub: "Promedio de los cierres diarios" },
  ];

  const share = async (mode: "copy" | "download") => {
    if (!r) return;
    setMsg(null);
    try {
      const blob = await sharePng(shareSvg(data, r, slotLabel(data, active), scopeLabel));
      if (mode === "copy" && navigator.clipboard && "ClipboardItem" in window) {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        setMsg("Imagen copiada: pégala en WhatsApp o en el correo.");
        return;
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ocupacion_flota_${r.ts.slice(0, 10)}_${active}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMsg(mode === "copy" ? "Tu navegador no permite copiar imágenes: se descargó." : "Imagen descargada.");
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  return (
    <div className={`space-y-4 ${isFetching ? "opacity-70 transition-opacity" : ""}`}>
      <section className="card grid gap-6 p-5 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <div className="flex flex-col items-center text-center">
          <div className="mb-2 inline-flex rounded-full border border-line p-0.5 text-xs" role="tablist" aria-label="Lectura">
            {(["manana", "ahora", "cierre"] as Slot[]).map((s) => (
              <button
                key={s}
                role="tab"
                aria-selected={active === s}
                disabled={!readingOf(data, s)}
                onClick={() => setSlot(s)}
                className={`rounded-full px-3 py-1 disabled:opacity-40 ${active === s ? "bg-brand font-medium text-brand-ink" : "text-ink-2"}`}
              >
                {s === "manana" ? "Mañana" : s === "ahora" ? "Ahora" : "Cierre"}
              </button>
            ))}
          </div>
          <Gauge pct={r?.pct ?? null} meta={data.meta} minimo={data.minimo} />
          <p className="-mt-1 text-5xl font-semibold tracking-tight">{pctText(r?.pct)}</p>
          <p className="mt-1 text-sm text-ink-2">{r ? `${intText(r.ocupadas)} de ${intText(r.flota)} unidades ocupadas` : "Sin datos"}</p>
          <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
            <StatusPill status={st} />
            <span className="text-xs text-muted">{slotLabel(data, active)}</span>
          </div>
          {r?.stale && <p className="mt-2 text-xs text-warn">Odoo no respondió: se muestra la última lectura guardada.</p>}
          <div className="mt-3 flex flex-wrap justify-center gap-2 print:hidden">
            <button className="btn-ghost border border-line text-xs" onClick={() => share("copy")} disabled={!r}>
              <IconCopy width={14} height={14} /> Copiar imagen
            </button>
            <button className="btn-ghost border border-line text-xs" onClick={() => share("download")} disabled={!r}>
              <IconDownload width={14} height={14} /> Descargar imagen
            </button>
            {onRefresh && (
              <button className="btn-ghost border border-line text-xs" onClick={onRefresh} disabled={isFetching} title="Vuelve a pedir los datos; la lectura de Odoo se renueva cada pocos minutos">
                <IconRefresh width={14} height={14} className={isFetching ? "animate-spin" : ""} /> Recargar
              </button>
            )}
          </div>
          {msg && <p className="mt-2 text-xs text-ink-2" role="status">{msg}</p>}
        </div>

        <div className="min-w-0 space-y-4">
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            {tiles.map((t) => {
              const d = t.key === active ? null : compare(t.value);
              return (
                <div key={t.key} className={`rounded-lg border p-3 ${t.key === active ? "border-brand bg-brand/5" : "border-line"}`}>
                  <p className="text-xs text-ink-2">{t.label}</p>
                  <p className="text-2xl font-semibold tracking-tight">{pctText(t.value)}</p>
                  <p className="text-xs text-muted">{t.sub}</p>
                  {d != null && (
                    <p className="mt-1 text-xs text-ink-2">
                      {d === 0 ? "Igual que" : `${d > 0 ? "+" : "−"}${Math.abs(d).toLocaleString("es-MX", { maximumFractionDigits: 1 })} pts vs`} {SLOT_SHORT[active]}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <div>
            <h3 className="mb-1 text-sm font-semibold">Ocupación al cierre de cada día (últimos {trend.length} días)</h3>
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend} margin={{ top: 8, right: 56, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke={colors.grid} vertical={false} />
                  <XAxis dataKey="label" stroke={colors.axis} tick={{ fill: colors.muted, fontSize: 11 }} tickLine={false} minTickGap={18} />
                  <YAxis
                    stroke={colors.axis}
                    axisLine={false}
                    tickLine={false}
                    width={40}
                    tick={{ fill: colors.muted, fontSize: 11 }}
                    tickFormatter={(v: number) => `${v}%`}
                    domain={[
                      (min: number) => Math.max(0, Math.floor(Math.min(min, data.minimo) - 5)),
                      (max: number) => Math.min(100, Math.ceil(Math.max(max, data.meta) + 5)),
                    ]}
                  />
                  <ReferenceLine y={data.meta} strokeDasharray="4 4" style={{ stroke: "rgb(var(--good))" }} label={{ value: `Meta ${data.meta}%`, position: "right", fontSize: 11, fill: colors.muted }} />
                  <ReferenceLine y={data.minimo} strokeDasharray="4 4" style={{ stroke: "rgb(var(--bad))" }} label={{ value: `Mín. ${data.minimo}%`, position: "right", fontSize: 11, fill: colors.muted }} />
                  <Tooltip
                    cursor={{ stroke: colors.axis }}
                    content={({ active: a, payload }) =>
                      a && payload?.length ? (
                        <div className="rounded-lg border border-line bg-raised px-3 py-2 text-xs shadow-md">
                          <p className="mb-1 font-medium text-ink">Cierre del {dayText(String(payload[0].payload.dia))}</p>
                          <p className="text-ink-2">
                            <span className="font-medium text-ink">{pctText(payload[0].payload.pct)}</span> · {intText(payload[0].payload.ocupadas)} de {intText(payload[0].payload.flota)}
                          </p>
                        </div>
                      ) : null
                    }
                  />
                  <Line type="monotone" dataKey="pct" name="Ocupación" stroke={colors.series[0]} strokeWidth={2} dot={false} activeDot={{ r: 4 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </section>

      {r && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <section className="card p-4">
            <h3 className="mb-2 text-sm font-semibold">Por almacén · {slotLabel(data, active).toLowerCase()}</h3>
            <ul className="divide-y divide-line/60">
              {r.almacenes.map((w) => {
                const s = statusOf(w.pct, data.meta, data.minimo);
                return (
                  <li key={String(w.id)} className="grid grid-cols-[minmax(0,7rem)_minmax(3rem,1fr)_4rem_4rem] items-center gap-3 py-1.5 text-sm sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)_4.5rem_5.5rem]">
                    <span className="truncate" title={w.nombre}>{w.nombre}</span>
                    <span className="relative h-2 rounded-full" style={{ background: `rgb(var(--${s}) / 0.15)` }} aria-hidden>
                      <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.min(100, w.pct ?? 0)}%`, background: `rgb(var(--${s}))` }} />
                    </span>
                    <span className="text-right font-medium tabular">{pctText(w.pct)}</span>
                    <span className="text-right text-xs text-muted tabular">{intText(w.ocupadas)}/{intText(w.flota)}</span>
                  </li>
                );
              })}
            </ul>
          </section>
          <section className="card p-4">
            <h3 className="mb-2 text-sm font-semibold">Unidades por estado</h3>
            <ul className="space-y-1 text-sm">
              {r.estados.map((e) => (
                <li key={e.grupo} className="flex justify-between gap-2">
                  <span className="text-ink-2">{e.grupo}</span>
                  <span className="font-medium tabular">{intText(e.unidades)}</span>
                </li>
              ))}
              <li className="flex justify-between gap-2 border-t border-line pt-1">
                <span className="font-medium">Flota de renta</span>
                <span className="font-semibold tabular">{intText(r.flota)}</span>
              </li>
            </ul>
            <p className="mt-3 text-xs text-muted">
              Ocupación = ({data.estados_ocupados.join(" + ")}) ÷ flota de renta
              {data.estados_excluidos.length ? `, sin ${data.estados_excluidos.join(", ")}` : ""}. Es el mismo cálculo de Rentas e inventario y del
              Histórico de flota. La lectura «ahora» se toma de Odoo › Flota y se reutiliza {data.minutos_vivo} min para no saturarlo; el cierre es el estado de
              cada equipo a las {data.hora_cierre}.
            </p>
          </section>
        </div>
      )}
    </div>
  );
}

const SLOT_SHORT: Record<Slot, string> = { manana: "la mañana", ahora: "ahora", cierre: "el cierre" };
