import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { useTheme } from "../hooks/useTheme";
import { BrandLogo } from "./BrandLogo";
import { FilterBar } from "./FilterBar";
import { canSee } from "../lib/permissions";
import type { ModuleName } from "../types/api";
import { IconCart, IconForecast, IconShield, IconMenu, IconMoon, IconRental, IconSales, IconSpark, IconSun, IconWrench } from "./Icons";

const NAV: { to: string; label: string; Icon: typeof IconSales; module?: ModuleName; ai?: boolean; admin?: boolean }[] = [
  { to: "/ventas", label: "Ventas y facturación", Icon: IconSales, module: "ventas" },
  { to: "/rentas", label: "Rentas e inventario", Icon: IconRental, module: "rentas" },
  { to: "/mantenimiento", label: "Mantenimiento", Icon: IconWrench, module: "mantenimiento" },
  { to: "/compras", label: "Compras", Icon: IconCart, module: "compras" },
  { to: "/pronostico", label: "Pronóstico", Icon: IconForecast, module: "pronostico" },
  { to: "/asistente", label: "Asistente IA", Icon: IconSpark, ai: true },
  { to: "/admin", label: "Administración", Icon: IconShield, admin: true },
];

export function Layout() {
  const { user, logout } = useAuth();
  const nav = NAV.filter((n) => (n.module ? canSee(user, n.module) : n.ai ? user?.ai : n.admin ? user?.is_admin : true));
  const [theme, toggleTheme] = useTheme();
  const [open, setOpen] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const { pathname } = useLocation();
  // Páginas sin barra de filtros
  const isAssistant = pathname.startsWith("/asistente") || pathname.startsWith("/admin") || pathname.startsWith("/sin-acceso");
  const pageTitle = pathname.startsWith("/admin") ? "Administración" : pathname.startsWith("/asistente") ? "Asistente IA" : "";

  return (
    <div className="flex min-h-full">
      {open && <div className="fixed inset-0 z-30 bg-black/40 lg:hidden" onClick={() => setOpen(false)} aria-hidden />}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col bg-side text-side-ink transition-transform print:hidden lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="px-5 pb-4 pt-6">
          <BrandLogo className="h-8 w-auto text-side-ink" isoColor="rgb(var(--accent))" />
          <div className="mt-2 text-[11px] font-semibold uppercase tracking-[0.18em] text-side-muted">Business Intelligence</div>
        </div>
        <nav className="flex-1 space-y-0.5 overflow-y-auto px-3 py-2" aria-label="Módulos">
          {nav.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive ? "bg-accent font-semibold text-accent-ink" : "text-side-ink/80 hover:bg-white/10 hover:text-side-ink"
                }`
              }
            >
              <Icon /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-side-line p-4 text-sm">
          <div className="mb-3 flex min-w-0 items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold uppercase text-accent-ink" aria-hidden>
              {(user?.name || user?.username || "?").slice(0, 1)}
            </span>
            <div className="min-w-0">
              <div className="truncate font-medium" title={user?.username}>{user?.name || user?.username}</div>
              <div className="truncate text-xs text-side-muted">
                {user?.role_label || user?.role}
                {user && (user.warehouse_ids.length > 0 || user.branch_ids.length > 0) && " · acceso limitado"}
              </div>
            </div>
          </div>
          <div className="flex gap-2">
            <button className="btn flex-1 border border-side-line text-side-ink hover:bg-white/10" onClick={toggleTheme} aria-label="Cambiar tema">
              {theme === "dark" ? <IconSun /> : <IconMoon />}
              {theme === "dark" ? "Claro" : "Oscuro"}
            </button>
            <button className="btn flex-1 border border-side-line text-side-ink hover:bg-white/10" onClick={logout}>
              Salir
            </button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-line bg-page/95 px-4 py-3 backdrop-blur print:hidden lg:px-6">
          <div className="flex items-start gap-3">
            <button className="btn-ghost -ml-2 lg:hidden" onClick={() => setOpen(true)} aria-label="Abrir menú">
              <IconMenu />
            </button>
            <BrandLogo variant="isotipo" className="mt-1.5 h-6 w-6 shrink-0 text-ink lg:hidden" isoColor="rgb(var(--brand))" />
            {isAssistant ? (
              <h1 className="py-1.5 text-base font-semibold">{pageTitle}</h1>
            ) : (
              <div className="min-w-0 flex-1">
                <button
                  className="btn-ghost border border-line lg:hidden"
                  onClick={() => setFiltersOpen((v) => !v)}
                  aria-expanded={filtersOpen}
                >
                  Filtros {filtersOpen ? "▴" : "▾"}
                </button>
                <div className={`${filtersOpen ? "mt-3 block" : "hidden"} lg:mt-0 lg:block`}>
                  <FilterBar />
                </div>
              </div>
            )}
          </div>
        </header>
        <main className="flex-1 px-4 py-5 lg:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
