import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, getToken, setToken, setUnauthorizedHandler } from "../api/client";
import type { User } from "../types/api";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  loginGoogle: (credential: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(Boolean(getToken()));
  const queryClient = useQueryClient();

  const logout = useCallback(() => {
    setToken(null);
    setUser(null);
    // Cada usuario puede tener otro alcance (almacén/sucursal): no reutilizar datos en caché.
    queryClient.clear();
  }, [queryClient]);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    if (!getToken()) return;
    api
      .me()
      .then(setUser)
      .catch(() => logout())
      .finally(() => setLoading(false));
  }, [logout]);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.login(username, password);
    queryClient.clear();
    setToken(res.access_token);
    setUser(res.user);
  }, [queryClient]);

  const loginGoogle = useCallback(async (credential: string) => {
    const res = await api.loginGoogle(credential);
    queryClient.clear();
    setToken(res.access_token);
    setUser(res.user);
  }, [queryClient]);

  const value = useMemo(() => ({ user, loading, login, loginGoogle, logout }), [user, loading, login, loginGoogle, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth fuera de AuthProvider");
  return ctx;
}
