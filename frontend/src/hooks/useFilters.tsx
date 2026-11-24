import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { presetRange, type PresetKey } from "../lib/dates";
import { load, save } from "../lib/storage";
import type { CompareMode, KpiFilters } from "../types/api";

interface Period {
  preset: PresetKey;
  dateFrom: string;
  dateTo: string;
}

interface FilterState extends Period {
  companyId: number | null;
  warehouseId: number | null;
  branchId: number | null;
  compare: CompareMode;
  iva: "sin" | "con";
}

interface Stored extends FilterState {
  /** El Histórico de flota tiene su propio periodo: por omisión, todo el histórico. */
  hist: Period;
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

function period(saved: Partial<Period> | undefined, fallback: PresetKey): Period {
  const preset = saved?.preset ?? fallback;
  // Los presets se recalculan cada vez (el "mes en curso" cambia con el tiempo).
  const range = presetRange(preset) ?? { from: saved?.dateFrom ?? "", to: saved?.dateTo ?? "" };
  const def = presetRange(fallback)!;
  return { preset, dateFrom: range.from || def.from, dateTo: range.to || def.to };
}

function initial(): Stored {
  const saved = load<Partial<Stored>>(STORAGE_KEY, {});
  return {
    // «Todo el histórico» solo aplica al Histórico de flota; en los demás módulos se usa el mes en curso.
    ...period(saved.preset === "all" ? undefined : saved, "this_month"),
    hist: period(saved.hist, "all"),
    companyId: saved.companyId ?? null,
    warehouseId: saved.warehouseId ?? null,
    branchId: saved.branchId ?? null,
    compare: saved.compare ?? "previous",
    iva: saved.iva === "con" ? "con" : "sin",
  };
}

export function FiltersProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState<Stored>(initial);
  const { pathname } = useLocation();
  const isHist = pathname.startsWith("/historico");
  // Comisiones se calcula por sucursal: el almacén no aplica (no se manda, para no mezclar vistas en caché).
  const noWarehouse = pathname.startsWith("/comisiones") || pathname.startsWith("/vendedores");

  useEffect(() => save(STORAGE_KEY, stored), [stored]);

  const value = useMemo<FiltersCtx>(() => {
    const { hist, ...general } = stored;
    const state: FilterState = isHist ? { ...general, ...hist } : general;
    const filters: KpiFilters = {
      date_from: state.dateFrom,
      date_to: state.dateTo,
      company_id: state.companyId,
      warehouse_id: noWarehouse ? null : state.warehouseId,
      branch_id: state.branchId,
      compare: state.compare,
      iva: state.iva,
    };
    const setPeriod = (fn: (p: Period) => Period) =>
      setStored((s) => (isHist ? { ...s, hist: fn(s.hist) } : { ...s, ...fn(s) }));
    return {
      state,
      filters,
      setPreset: (p) =>
        setPeriod((s) => {
          const r = presetRange(p);
          return r ? { preset: p, dateFrom: r.from, dateTo: r.to } : { ...s, preset: p };
        }),
      setRange: (from, to) => setPeriod(() => ({ preset: "custom", dateFrom: from, dateTo: to })),
      update: (patch) => {
        const { preset, dateFrom, dateTo, ...rest } = patch;
        if (preset !== undefined || dateFrom !== undefined || dateTo !== undefined)
          setPeriod((s) => ({ preset: preset ?? s.preset, dateFrom: dateFrom ?? s.dateFrom, dateTo: dateTo ?? s.dateTo }));
        setStored((s) => ({ ...s, ...rest }));
      },
    };
  }, [stored, isHist, noWarehouse]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useFilters(): FiltersCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useFilters fuera de FiltersProvider");
  return ctx;
}
