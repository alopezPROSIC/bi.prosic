import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { EmptyState } from "./components/States";
import { useAuth } from "./hooks/useAuth";
import { FiltersProvider } from "./hooks/useFilters";
import { canSee, firstAllowedPath, MODULE_ORDER } from "./lib/permissions";
import { AssistantPage } from "./pages/AssistantPage";
import { LoginPage } from "./pages/LoginPage";
import { ModulePage } from "./pages/ModulePage";

// El panel de administración solo lo descargan los administradores.
const AdminPage = lazy(() => import("./pages/AdminPage"));

function Protected() {
  const { user, loading } = useAuth();
  if (loading) return <div className="p-8 text-sm text-muted">Cargando…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return (
    <FiltersProvider>
      <Layout />
    </FiltersProvider>
  );
}

function Guard({ allowed, children }: { allowed: boolean; children: ReactNode }) {
  return allowed ? <>{children}</> : <NoAccess />;
}

function NoAccess() {
  return (
    <EmptyState title="Sin acceso">
      Tu rol no tiene acceso a esta sección. Si lo necesitas, pídelo a un administrador.
    </EmptyState>
  );
}

function Home() {
  const { user } = useAuth();
  return <Navigate to={user ? firstAllowedPath(user) : "/login"} replace />;
}

export default function App() {
  const { user } = useAuth();
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Protected />}>
        {MODULE_ORDER.map((m) => (
          <Route
            key={m}
            path={`/${m}`}
            element={
              <Guard allowed={canSee(user, m)}>
                <ModulePage key={m} module={m} />
              </Guard>
            }
          />
        ))}
        <Route path="/asistente" element={<Guard allowed={Boolean(user?.ai)}><AssistantPage /></Guard>} />
        <Route
          path="/admin"
          element={
            <Guard allowed={Boolean(user?.is_admin)}>
              <Suspense fallback={<div className="skeleton h-40 w-full" />}>
                <AdminPage />
              </Suspense>
            </Guard>
          }
        />
        <Route path="/sin-acceso" element={<NoAccess />} />
      </Route>
      <Route path="*" element={<Home />} />
    </Routes>
  );
}
