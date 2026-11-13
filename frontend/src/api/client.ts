import type {
  AccessEvent,
  AdminRole,
  AdminSettings,
  AdminUser,
  AiAnalysis,
  AuthConfig,
  FilterOptions,
  FleetQuery,
  FleetRecords,
  KpiFilters,
  ModuleKpis,
  ModuleName,
  TokenResponse,
  User,
  UserStatus,
} from "../types/api";

const TOKEN_KEY = "prosic-token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* almacenamiento no disponible: la sesión vive solo en memoria */
  }
}

function detailMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String(x.msg) : String(x))).join("; ");
  }
  if (status === 502) return "Odoo no respondió. Intenta de nuevo en unos momentos.";
  if (status === 429) return "Demasiadas solicitudes. Espera un momento.";
  return `Error ${status}`;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && token) {
    setToken(null);
    onUnauthorized();
  }
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* sin cuerpo JSON */
    }
    throw new ApiError(res.status, detailMessage(body, res.status));
  }
  return res;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(path, init);
  return (await res.json()) as T;
}

export function filtersToQuery(f: KpiFilters): string {
  const p = new URLSearchParams({ date_from: f.date_from, date_to: f.date_to, compare: f.compare });
  if (f.company_id) p.set("company_id", String(f.company_id));
  if (f.warehouse_id) p.set("warehouse_id", String(f.warehouse_id));
  if (f.branch_id) p.set("branch_id", String(f.branch_id));
  return p.toString();
}

const put = (body: unknown): RequestInit => ({ method: "PUT", body: JSON.stringify(body) });

function fleetQuery(q: FleetQuery): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== null && v !== undefined && v !== "") p.set(k, String(v));
  return p.toString();
}

export const api = {
  authConfig: () => json<AuthConfig>("/api/auth/config"),
  loginGoogle: (credential: string) =>
    json<TokenResponse>("/api/auth/google", { method: "POST", body: JSON.stringify({ credential }) }),
  login: (username: string, password: string) =>
    json<TokenResponse>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  admin: {
    users: () => json<{ users: AdminUser[]; statuses: UserStatus[]; admin_emails: string[]; domain: string }>("/api/admin/users"),
    saveUser: (u: Partial<AdminUser> & { email: string }) => json<AdminUser>("/api/admin/users", put(u)),
    deleteUser: (email: string) => json<{ ok: boolean }>(`/api/admin/users/${encodeURIComponent(email)}`, { method: "DELETE" }),
    roles: () => json<{ roles: AdminRole[]; modules: ModuleName[] }>("/api/admin/roles"),
    saveRole: (r: AdminRole) => json<AdminRole>("/api/admin/roles", put(r)),
    deleteRole: (name: string) => json<{ ok: boolean }>(`/api/admin/roles/${encodeURIComponent(name)}`, { method: "DELETE" }),
    settings: () => json<AdminSettings>("/api/admin/settings"),
    saveSettings: (values: Record<string, unknown>) => json<AdminSettings>("/api/admin/settings", put(values)),
    accessLog: (limit = 300) => json<{ events: AccessEvent[] }>(`/api/admin/access-log?limit=${limit}`),
    summaryStatus: () => json<{ email_enabled: boolean; recipients: string[] }>("/api/admin/summary/status"),
    sendSummary: () => json<{ sent_to: string[]; subject: string }>("/api/admin/summary/send", { method: "POST" }),
  },
  me: () => json<User>("/api/auth/me"),
  filterOptions: () => json<FilterOptions>("/api/odoo/filters"),
  kpis: (module: ModuleName, f: KpiFilters, refresh = false) =>
    json<ModuleKpis>(`/api/kpis/${module}?${filtersToQuery(f)}${refresh ? "&refresh=true" : ""}`),
  /** Registros del histórico de flota (tabla propia del sistema), paginados y filtrados. */
  fleetRecords: (q: FleetQuery, page: number, pageSize = 50) =>
    json<FleetRecords>(`/api/historico/registros?${fleetQuery(q)}&page=${page}&page_size=${pageSize}`),
  /** Descarga los registros filtrados (CSV). Se descarga con el token, sin exponerlo en la URL. */
  downloadFleetRecords: async (q: FleetQuery) => {
    const res = await apiFetch(`/api/historico/flota.csv?${fleetQuery(q)}`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `historico_flota_${q.formato}_${q.desde}_${q.hasta}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  syncFleetHistory: () =>
    json<{ events?: number; moves?: number; rows?: number; seconds?: number; running?: boolean }>("/api/historico/sincronizar", { method: "POST" }),
  analyze: (modulo: ModuleName, filtros: KpiFilters) =>
    json<AiAnalysis>("/api/ai/analyze", { method: "POST", body: JSON.stringify({ modulo, filtros }) }),
};
