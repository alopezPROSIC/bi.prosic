import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useFilters } from "../hooks/useFilters";
import { useAnalyze } from "../hooks/useKpis";
import type { ModuleName, Severity } from "../types/api";
import { IconAlert, IconCritical, IconInfo, IconSpark } from "./Icons";

const SEVERITY: Record<Severity, { label: string; cls: string; Icon: typeof IconInfo }> = {
  info: { label: "Info", cls: "border-brand/40 text-brand", Icon: IconInfo },
  alerta: { label: "Alerta", cls: "border-warn/50 text-warn", Icon: IconAlert },
  critico: { label: "Crítico", cls: "border-bad/50 text-bad", Icon: IconCritical },
};
const PRIORITY: Record<string, string> = { alta: "text-bad", media: "text-warn", baja: "text-muted" };

export function AiPanel({ module }: { module: ModuleName }) {
  const { filters } = useFilters();
  const analyze = useAnalyze(module);
  const navigate = useNavigate();
  const data = analyze.data;

  // Un análisis corresponde a unos filtros concretos: al cambiarlos, se descarta.
  const filterKey = JSON.stringify(filters);
  const { reset } = analyze;
  useEffect(() => {
    reset();
  }, [filterKey, module, reset]);

  return (
    <aside className="card flex flex-col gap-4 p-4" aria-live="polite">
      <header className="flex items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <IconSpark className="text-brand" /> Análisis IA
        </h2>
        <button className="btn-primary px-3 py-1.5" onClick={() => analyze.mutate()} disabled={analyze.isPending}>
          {analyze.isPending ? "Analizando…" : data ? "Volver a analizar" : "Analizar"}
        </button>
      </header>

      {!data && !analyze.isPending && !analyze.isError && (
        <p className="text-sm text-ink-2">
          Genera un resumen con hallazgos y recomendaciones a partir de los KPIs filtrados. Solo se envían datos agregados; los nombres de
          terceros se anonimizan.
        </p>
      )}
      {analyze.isPending && (
        <div className="space-y-2" aria-busy="true">
          <div className="skeleton h-3 w-full" />
          <div className="skeleton h-3 w-5/6" />
          <div className="skeleton h-3 w-4/6" />
          <div className="skeleton mt-4 h-16 w-full" />
          <div className="skeleton h-16 w-full" />
        </div>
      )}
      {analyze.isError && <p className="text-sm text-bad">{(analyze.error as Error).message}</p>}

      {data && (
        <div className="space-y-5 text-sm">
          <p className="whitespace-pre-line leading-relaxed">{data.resumen}</p>

          {data.hallazgos.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-2">Hallazgos</h3>
              <ul className="space-y-2">
                {data.hallazgos.map((h, i) => {
                  const s = SEVERITY[h.severidad];
                  return (
                    <li key={i} className="rounded-lg border border-line p-3">
                      <div className="mb-1 flex items-start gap-2">
                        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${s.cls}`}>
                          <s.Icon width={12} height={12} /> {s.label}
                        </span>
                        <span className="font-medium">{h.titulo}</span>
                      </div>
                      <p className="text-ink-2">{h.detalle}</p>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {data.recomendaciones.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-2">Recomendaciones</h3>
              <ol className="space-y-2">
                {data.recomendaciones.map((r, i) => (
                  <li key={i} className="rounded-lg bg-line/30 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <span className="font-medium">{r.accion}</span>
                      <span className={`shrink-0 text-xs font-semibold uppercase ${PRIORITY[r.prioridad]}`}>{r.prioridad}</span>
                    </div>
                    <p className="mt-1 text-ink-2">{r.impacto_esperado}</p>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {data.preguntas_sugeridas.length > 0 && (
            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-2">Profundizar en el asistente</h3>
              <div className="flex flex-wrap gap-2">
                {data.preguntas_sugeridas.map((q) => (
                  <button
                    key={q}
                    className="rounded-full border border-line px-3 py-1 text-left text-xs text-ink-2 hover:border-brand hover:text-ink"
                    onClick={() => navigate("/asistente", { state: { question: q } })}
                  >
                    {q}
                  </button>
                ))}
              </div>
            </section>
          )}
          <p className="text-xs text-muted">
            {data.cached ? "Resultado en caché · " : ""}
            {data.model ?? ""}
          </p>
        </div>
      )}
    </aside>
  );
}
