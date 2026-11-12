import { useFilters } from "../hooks/useFilters";
import { useFilterOptions } from "../hooks/useKpis";
import { PRESETS, type PresetKey } from "../lib/dates";
import type { CompareMode } from "../types/api";

const toId = (v: string) => (v ? Number(v) : null);

export function FilterBar() {
  const { state, setPreset, setRange, update } = useFilters();
  const { data: opts } = useFilterOptions();
  const warehouses = (opts?.warehouses ?? []).filter((w) => !state.companyId || !w.company_id || w.company_id === state.companyId);
  // Usuario limitado a un almacén/sucursal: el backend lo fuerza; aquí solo se muestra bloqueado.
  const lockedWh = opts?.scope?.warehouse_id ?? null;
  const lockedBr = opts?.scope?.branch_id ?? null;

  return (
    <div className="flex flex-wrap items-end gap-2 text-sm" role="group" aria-label="Filtros globales">
      <label className="flex flex-col gap-0.5">
        <span className="text-xs text-ink-2">Periodo</span>
        <select className="input" value={state.preset} onChange={(e) => setPreset(e.target.value as PresetKey)}>
          {PRESETS.map((p) => (
            <option key={p.key} value={p.key}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-xs text-ink-2">Desde</span>
        <input type="date" className="input" value={state.dateFrom} max={state.dateTo} onChange={(e) => e.target.value && setRange(e.target.value, state.dateTo)} />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-xs text-ink-2">Hasta</span>
        <input type="date" className="input" value={state.dateTo} min={state.dateFrom} onChange={(e) => e.target.value && setRange(state.dateFrom, e.target.value)} />
      </label>
      {(opts?.companies.length ?? 0) > 1 && (
        <label className="flex flex-col gap-0.5">
          <span className="text-xs text-ink-2">Compañía</span>
          <select className="input max-w-[12rem]" value={state.companyId ?? ""} onChange={(e) => update({ companyId: toId(e.target.value), warehouseId: null })}>
            <option value="">Todas</option>
            {opts!.companies.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {(opts?.branches.length ?? 0) > 0 && (
        <label className="flex flex-col gap-0.5">
          <span className="text-xs text-ink-2">Sucursal</span>
          <select
            className="input max-w-[12rem]"
            value={lockedBr ?? state.branchId ?? ""}
            disabled={lockedBr !== null}
            title={lockedBr !== null ? "Tu usuario está limitado a esta sucursal" : undefined}
            onChange={(e) => update({ branchId: toId(e.target.value) })}
          >
            {lockedBr === null && <option value="">{opts?.scope?.branch_ids?.length ? "Todas las mías" : "Todas"}</option>}
            {opts!.branches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>
      )}
      <label className="flex flex-col gap-0.5">
        <span className="text-xs text-ink-2">Almacén</span>
        <select
          className="input max-w-[12rem]"
          value={lockedWh ?? state.warehouseId ?? ""}
          disabled={lockedWh !== null}
          title={lockedWh !== null ? "Tu usuario está limitado a este almacén" : undefined}
          onChange={(e) => update({ warehouseId: toId(e.target.value) })}
        >
          {lockedWh === null && <option value="">{opts?.scope?.warehouse_ids?.length ? "Todos los míos" : "Todos"}</option>}
          {warehouses.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-xs text-ink-2">Comparar con</span>
        <select className="input" value={state.compare} onChange={(e) => update({ compare: e.target.value as CompareMode })}>
          <option value="previous">Periodo anterior</option>
          <option value="year">Mismo periodo año anterior</option>
          <option value="none">Sin comparación</option>
        </select>
      </label>
    </div>
  );
}
