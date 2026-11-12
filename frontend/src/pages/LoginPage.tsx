import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { api } from "../api/client";
import { BrandLogo } from "../components/BrandLogo";
import { useAuth } from "../hooks/useAuth";
import { firstAllowedPath } from "../lib/permissions";

// Google Identity Services (botón «Iniciar sesión con Google»).
interface GoogleId {
  initialize: (cfg: Record<string, unknown>) => void;
  renderButton: (el: HTMLElement, opts: Record<string, unknown>) => void;
}
declare global {
  interface Window {
    google?: { accounts: { id: GoogleId } };
  }
}

const GIS_SRC = "https://accounts.google.com/gsi/client";

function loadGis(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${GIS_SRC}"]`);
    const s = existing ?? Object.assign(document.createElement("script"), { src: GIS_SRC, async: true, defer: true });
    s.addEventListener("load", () => resolve());
    s.addEventListener("error", () => reject(new Error("No se pudo cargar el inicio de sesión de Google")));
    if (!existing) document.head.appendChild(s);
  });
}

export function LoginPage() {
  const { user, login, loginGoogle } = useAuth();
  const { data: cfg, isLoading } = useQuery({ queryKey: ["auth-config"], queryFn: api.authConfig, staleTime: Infinity });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const buttonRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!cfg?.google_client_id || !buttonRef.current) return;
    let cancelled = false;
    loadGis()
      .then(() => {
        if (cancelled || !buttonRef.current || !window.google) return;
        window.google.accounts.id.initialize({
          client_id: cfg.google_client_id,
          hd: cfg.allowed_domain, // sugiere solo cuentas del dominio (el backend lo valida de todos modos)
          ux_mode: "popup",
          callback: async (resp: { credential?: string }) => {
            if (!resp.credential) return;
            setBusy(true);
            setError(null);
            try {
              await loginGoogle(resp.credential);
            } catch (err) {
              setError((err as Error).message);
            } finally {
              setBusy(false);
            }
          },
        });
        window.google.accounts.id.renderButton(buttonRef.current, {
          theme: "outline",
          size: "large",
          text: "signin_with",
          shape: "rectangular",
          locale: "es-419",
          width: 300,
        });
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [cfg, loginGoogle]);

  if (user) return <Navigate to={firstAllowedPath(user)} replace />;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-full flex-col items-center justify-center gap-8 overflow-hidden bg-[#154046] p-4">
      {/* isotipo de fondo, como en las piezas de marca */}
      <BrandLogo variant="isotipo" className="pointer-events-none absolute -right-24 -top-16 h-[28rem] w-auto text-[#33e989] opacity-[0.07]" />
      <div className="relative text-center">
        <BrandLogo className="mx-auto h-12 w-auto text-[#f4fff3]" isoColor="#33e989" />
        <p className="mt-3 text-xs font-semibold uppercase tracking-[0.22em] text-[#33e989]">Business Intelligence</p>
      </div>
      <div className="card relative w-full max-w-sm space-y-5 border-0 p-6 shadow-xl">
        <div>
          <h1 className="text-lg font-semibold">Iniciar sesión</h1>
          <p className="text-xs text-ink-2">Indicadores de ventas, rentas, mantenimiento y compras</p>
        </div>

        {isLoading && <div className="skeleton h-10 w-full" />}

        {cfg?.google_client_id && (
          <div className="space-y-2">
            <p className="text-sm text-ink-2">Entra con tu cuenta de Google @{cfg.allowed_domain}.</p>
            <div ref={buttonRef} className="flex min-h-[44px] justify-center" aria-busy={busy} />
          </div>
        )}

        {cfg?.password_login && (
          <form onSubmit={submit} className="space-y-4">
            {cfg.google_client_id && <p className="border-t border-line pt-4 text-xs text-muted">Acceso de emergencia</p>}
            <label className="block space-y-1">
              <span className="text-sm text-ink-2">Usuario</span>
              <input className="input w-full" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-ink-2">Contraseña</span>
              <input
                className="input w-full"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </label>
            <button className="btn-primary w-full" disabled={busy}>
              {busy ? "Entrando…" : "Entrar"}
            </button>
          </form>
        )}

        {cfg && !cfg.google_client_id && !cfg.password_login && (
          <p className="text-sm text-bad">No hay ningún método de acceso configurado. Revisa GOOGLE_CLIENT_ID en el servidor.</p>
        )}

        {error && (
          <p role="alert" className="text-sm text-bad">
            {error}
          </p>
        )}
      </div>
      <p className="relative text-xs text-[#ebeedc]/60">Uso interno de PROSIC</p>
    </div>
  );
}
