import type { ColumnType, Unit } from "../types/api";

const mxn = new Intl.NumberFormat("es-MX", { style: "currency", currency: "MXN", maximumFractionDigits: 0 });
const mxnCompact = new Intl.NumberFormat("es-MX", {
  style: "currency",
  currency: "MXN",
  notation: "compact",
  maximumFractionDigits: 1,
});
const int = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 0 });
const dec1 = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 1 });

export function formatValue(v: number | null | undefined, unit: Unit, compact = false): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  switch (unit) {
    case "MXN":
      return compact && Math.abs(v) >= 100_000 ? mxnCompact.format(v) : mxn.format(v);
    case "%":
      return `${dec1.format(v)} %`;
    case "days":
      return `${dec1.format(v)} días`;
    default:
      return Number.isInteger(v) ? int.format(v) : dec1.format(v);
  }
}

export function formatAxis(v: number, unit: Unit): string {
  if (unit === "MXN") return mxnCompact.format(v);
  if (unit === "%") return `${dec1.format(v)}%`;
  return new Intl.NumberFormat("es-MX", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function formatCell(v: unknown, type: ColumnType): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") {
    if (type === "money") return mxn.format(v);
    if (type === "percent") return `${dec1.format(v)} %`;
    if (type === "days") return `${dec1.format(v)} d`;
    return Number.isInteger(v) ? int.format(v) : dec1.format(v);
  }
  if (type === "date" && typeof v === "string") {
    const [y, m, d] = v.split("-");
    return d ? `${d}/${m}/${y}` : v;
  }
  return String(v);
}

export function formatDate(iso: string): string {
  return formatCell(iso, "date");
}
