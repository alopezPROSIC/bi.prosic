import type { ModuleName, Permission, User } from "../types/api";

export const MODULE_ORDER: ModuleName[] = ["ventas", "comisiones", "rentas", "historico", "mantenimiento", "compras", "pronostico"];

export function canSee(user: User | null, module: Permission): boolean {
  return Boolean(user && (user.is_admin || user.modules.includes(module)));
}

/** Primera página a la que el usuario tiene acceso (para redirigir tras el login). */
export function firstAllowedPath(user: User): string {
  const m = MODULE_ORDER.find((x) => canSee(user, x));
  if (m) return `/${m}`;
  if (canSee(user, "datos")) return "/datos";
  if (user.ai) return "/asistente";
  if (user.is_admin) return "/admin";
  return "/sin-acceso";
}

export const PERMISSION_TITLES: Record<Permission, string> = {
  ventas: "Ventas y facturación",
  comisiones: "Comisiones",
  rentas: "Rentas e inventario",
  historico: "Histórico de flota",
  mantenimiento: "Mantenimiento",
  compras: "Compras y proveedores",
  pronostico: "Pronóstico",
  datos: "Datos de Odoo",
};
