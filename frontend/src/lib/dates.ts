export type PresetKey = "this_month" | "last_month" | "this_quarter" | "this_year" | "last_12" | "custom";

export const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "this_month", label: "Este mes" },
  { key: "last_month", label: "Mes anterior" },
  { key: "this_quarter", label: "Este trimestre" },
  { key: "this_year", label: "Este año" },
  { key: "last_12", label: "Últimos 12 meses" },
  { key: "custom", label: "Personalizado" },
];

export function iso(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function presetRange(key: PresetKey, today = new Date()): { from: string; to: string } | null {
  const y = today.getFullYear();
  const m = today.getMonth();
  switch (key) {
    case "this_month":
      return { from: iso(new Date(y, m, 1)), to: iso(today) };
    case "last_month":
      return { from: iso(new Date(y, m - 1, 1)), to: iso(new Date(y, m, 0)) };
    case "this_quarter": {
      const q = Math.floor(m / 3) * 3;
      return { from: iso(new Date(y, q, 1)), to: iso(today) };
    }
    case "this_year":
      return { from: iso(new Date(y, 0, 1)), to: iso(today) };
    case "last_12":
      return { from: iso(new Date(y, m - 11, 1)), to: iso(today) };
    default:
      return null;
  }
}
