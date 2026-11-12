import type { Column } from "../types/api";

function cell(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  // Evita inyección de fórmulas al abrir en Excel
  const safe = /^[=+\-@\t\r]/.test(s) && typeof v !== "number" ? `'${s}` : s;
  return /[",\n;]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export function downloadCsv(filename: string, columns: Pick<Column, "key" | "label">[], rows: Record<string, unknown>[]) {
  const lines = [columns.map((c) => cell(c.label)).join(","), ...rows.map((r) => columns.map((c) => cell(r[c.key])).join(","))];
  // BOM para que Excel abra correctamente los acentos
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.endsWith(".csv") ? filename : `${filename}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
