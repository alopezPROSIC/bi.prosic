import { useMemo } from "react";
import { useTheme } from "./useTheme";

// Paleta categórica validada (orden fijo, nunca ciclada) — ver index.css.
// Recharts necesita colores concretos en atributos SVG, así que se leen de las variables CSS.
export interface ChartColors {
  series: string[];
  previous: string;
  grid: string;
  axis: string;
  muted: string;
  ink: string;
  surface: string;
}

function cssVar(name: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return raw.includes(" ") ? `rgb(${raw.split(/\s+/).join(",")})` : raw;
}

export function useChartColors(): ChartColors {
  const [theme] = useTheme();
  return useMemo(
    () => ({
      series: Array.from({ length: 8 }, (_, i) => cssVar(`--series-${i + 1}`)),
      previous: cssVar("--series-prev"),
      grid: cssVar("--grid"),
      axis: cssVar("--axis"),
      muted: cssVar("--muted"),
      ink: cssVar("--ink"),
      surface: cssVar("--surface"),
    }),
    // Se recalcula al cambiar el tema.
    [theme],
  );
}
