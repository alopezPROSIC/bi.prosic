import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { AiPanel } from "../components/AiPanel";
import { IconPrint, IconRefresh } from "../components/Icons";
import { ModuleView } from "../components/ModuleView";
import { CardsSkeleton, ChartSkeleton, ErrorState } from "../components/States";
import { useAuth } from "../hooks/useAuth";
import { useFilters } from "../hooks/useFilters";
import { useKpis } from "../hooks/useKpis";
import { formatDate } from "../lib/format";
import type { ModuleName } from "../types/api";

export const MODULE_TITLES: Record<ModuleName, string> = {
  pronostico: "Pronóstico a 6 meses",
  ventas: "Ventas y facturación",
  rentas: "Rentas e inventario",
  mantenimiento: "Mantenimiento",
  compras: "Compras y proveedores",
};

/** Imprime / guarda como PDF en tema claro (las gráficas se repintan con la paleta clara antes de imprimir). */
function printPdf() {
  const root = document.documentElement;
  const wasDark = root.classList.contains("dark");
  const restore = () => {
    if (wasDark) root.classList.add("dark");
    window.removeEventListener("afterprint", restore);
  };
  if (wasDark) root.classList.remove("dark");
  window.addEventListener("afterprint", restore);
  // Espera a que React repinte las gráficas con los colores claros.
  window.setTimeout(() => window.print(), wasDark ? 250 : 0);
}

export function ModulePage({ module }: { module: ModuleName }) {
  const { data, isLoading, isError, error, refetch, isFetching } = useKpis(module);
  const { filters } = useFilters();
  const { user } = useAuth();
  const qc = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const forceRefresh = async () => {
    setRefreshing(true);
    try {
      const fresh = await api.kpis(module, filters, true);
      qc.setQueryData(["kpis", module, filters], fresh);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_380px] print:block">
      <div className="min-w-0 space-y-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{MODULE_TITLES[module]}</h1>
            {data && (
              <p className="text-xs text-muted">
                {formatDate(data.period.date_from)} – {formatDate(data.period.date_to)}
                {data.previous_period && ` · vs ${formatDate(data.previous_period.date_from)} – ${formatDate(data.previous_period.date_to)}`}
                {" · actualizado "}
                {new Date(data.generated_at).toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" })}
              </p>
            )}
          </div>
          <div className="flex gap-1 print:hidden">
            <button className="btn-ghost text-xs" onClick={printPdf} disabled={!data} title="Imprimir o guardar como PDF">
              <IconPrint width={14} height={14} /> PDF
            </button>
            <button className="btn-ghost text-xs" onClick={forceRefresh} disabled={refreshing || isFetching} title="Consultar Odoo sin caché">
              <IconRefresh width={14} height={14} className={refreshing || isFetching ? "animate-spin" : ""} /> Actualizar
            </button>
          </div>
        </div>

        {isLoading ? (
          <div className="space-y-4">
            <CardsSkeleton />
            <div className="grid gap-4 xl:grid-cols-2">
              <ChartSkeleton />
              <ChartSkeleton />
            </div>
          </div>
        ) : isError ? (
          <ErrorState message={(error as Error).message} onRetry={() => refetch()} />
        ) : data ? (
          <div className={isFetching ? "opacity-60 transition-opacity" : ""}>
            <ModuleView data={data} />
          </div>
        ) : null}
      </div>
      {user?.ai && (
        <div className="2xl:sticky 2xl:top-24 2xl:self-start print:mt-4 print:break-before-page">
          <AiPanel module={module} />
        </div>
      )}
    </div>
  );
}
