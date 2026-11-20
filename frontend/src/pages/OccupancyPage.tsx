import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { OccupancyBoard } from "../components/OccupancyGauge";
import { useFilterOptions } from "../hooks/useKpis";

/** Ocupación diaria de la flota: tacómetro con el corte de la mañana, la lectura del momento y el cierre de la noche. */
export function OccupancyPage() {
  const { data: opts } = useFilterOptions();
  const qc = useQueryClient();
  const locked = opts?.scope?.warehouse_id ?? null;
  const [wh, setWh] = useState<number | null>(null);
  const warehouseId = locked ?? wh;
  const name = (opts?.warehouses ?? []).find((w) => w.id === warehouseId)?.name;
  const scopeLabel = name ?? (opts?.scope?.warehouse_ids?.length ? "Mis almacenes" : "Toda la flota");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Ocupación diaria de la flota</h1>
          <p className="text-xs text-muted">Corte de la mañana, lectura del momento y cierre de la noche · {scopeLabel}</p>
        </div>
        <label className="flex flex-col gap-0.5 text-sm">
          <span className="text-xs text-ink-2">Almacén</span>
          <select
            className="input max-w-[14rem]"
            value={warehouseId ?? ""}
            disabled={locked !== null}
            title={locked !== null ? "Tu usuario está limitado a este almacén" : undefined}
            onChange={(e) => setWh(e.target.value ? Number(e.target.value) : null)}
          >
            {locked === null && <option value="">{opts?.scope?.warehouse_ids?.length ? "Todos los míos" : "Toda la flota"}</option>}
            {(opts?.warehouses ?? []).map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <OccupancyBoard
        warehouseId={warehouseId}
        scopeLabel={scopeLabel}
        onRefresh={() => qc.invalidateQueries({ queryKey: ["ocupacion"] })}
      />
    </div>
  );
}
