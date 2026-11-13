import type { ModuleName, User } from "../types/api";

export const MODULE_ORDER: ModuleName[] = ["ventas", "comisiones", "rentas", "historico", "mantenimiento", "compras", "pronostico"];

export function canSee(user: User | null, module: ModuleName): boolean {
  return Boolean(user && (user.is_admin || user.modules.includes(module)));
}

/** Primera página a la que el usuario tiene acceso (para redirigir tras el login). */
export function firstAllowedPath(user: User): string {
  const m = MODULE_ORDER.find((x) => canSee(user, x));
  if (m) return `/${m}`;
  if (user.ai) return "/asistente";
  if (user.is_admin) return "/admin";
  return "/sin-acceso";
}
