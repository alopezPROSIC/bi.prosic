import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { presetRange, type PresetKey } from "../lib/dates";
import { load, save } from "../lib/storage";
import type { CompareMode, KpiFilters } from "../types/api";

interface FilterState {
  preset: PresetKey;
  dateFrom: string;
  dateTo: string;
  companyId: number | null;
  warehouseId: number | null;
  branchId: number | null;
  compare: CompareMode;
}

interface FiltersCtx {
  state: FilterState;
  filters: KpiFilters;
  setPreset: (p: PresetKey) => void;
  setRange: (from: string, to: string) => void;
  update: (patch: Partial<FilterState>) => void;
}

const STORAGE_KEY = "prosic-filters";
const Ctx = createContext<FiltersCtx | null>(null);

function initial(): FilterState {
  const saved = load<Partial<FilterState>>(STORAGE_KEY, {});
  const preset = saved.preset ?? "this_month";
  // Los presets se recalculan cada vez (el "mes en curso" cambia con el tiempo).
  const range = presetRange(preset) ?? { from: saved.dateFrom ?? "", to: saved.dateTo ?? "" };
  const fallback = presetRange("this_month")!;
  return {
    preset,
    dateFrom: range.from || fallback.from,
    dateTo: range.to || fallback.to,
    companyId: saved.companyId ?? null,
    warehouseId: saved.warehouseId ?? null,
    branchId: saved.branchId ?? null,
    compare: saved.compare ?? "previous",
  };
}

export function FiltersProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<FilterState>(initial);

  useEffect(() => save(STORAGE_KEY, state), [state]);

  const value = useMemo<FiltersCtx>(() => {
    const filters: KpiFilters = {
      date_from: state.dateFrom,
      date_to: state.dateTo,
      company_id: state.companyId,
      warehouse_id: state.warehouseId,
      branch_id: state.branchId,
      compare: state.compare,
    };
    return {
      state,
      filters,
      setPreset: (p) =>
        setState((s) => {
          const r = presetRange(p);
          return r ? { ...s, preset: p, dateFrom: r.from, dateTo: r.to } : { ...s, preset: p };
        }),
      setRange: (from, to) => setState((s) => ({ ...s, preset: "custom", dateFrom: from, dateTo: to })),
      update: (patch) => setState((s) => ({ ...s, ...patch })),
    };
  }, [state]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useFilters(): FiltersCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useFilters fuera de FiltersProvider");
  return ctx;
}
