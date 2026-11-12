import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModuleName } from "../types/api";
import { useFilters } from "./useFilters";

export function useKpis(module: ModuleName) {
  const { filters } = useFilters();
  return useQuery({
    queryKey: ["kpis", module, filters],
    queryFn: () => api.kpis(module, filters),
    staleTime: 5 * 60_000,
    placeholderData: (prev) => (prev?.module === module ? prev : undefined),
  });
}

export function useFilterOptions() {
  return useQuery({ queryKey: ["filter-options"], queryFn: api.filterOptions, staleTime: 60 * 60_000 });
}

export function useAnalyze(module: ModuleName) {
  const { filters } = useFilters();
  return useMutation({ mutationKey: ["analyze", module, filters], mutationFn: () => api.analyze(module, filters) });
}
