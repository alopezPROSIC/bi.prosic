import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { ErrorState } from "../components/States";
import { useAuth } from "../hooks/useAuth";
import { useFilterOptions } from "../hooks/useKpis";
import { MODULE_TITLES } from "./ModulePage";
import type { AdminRole, AdminUser, FilterOption, ModuleName, UserStatus } from "../types/api";

type Tab = "usuarios" | "roles" | "config" | "accesos";
const TABS: { key: Tab; label: string }[] = [
  { key: "usuarios", label: "Usuarios" },
  { key: "roles", label: "Roles y vistas" },
  { key: "config", label: "Configuración" },
  { key: "accesos", label: "Bitácora de accesos" },
];
const STATUS_LABEL: Record<UserStatus, string> = { activo: "Activo", pendiente: "Pendiente", bloqueado: "Bloqueado" };
const STATUS_CLASS: Record<UserStatus, string> = {
  activo: "bg-good/15 text-good",
  pendiente: "bg-warn/15 text-warn",
  bloqueado: "bg-bad/15 text-bad",
};

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-MX", { dateStyle: "short", timeStyle: "short" });
}

function names(ids: number[], opts: FilterOption[] | undefined) {
  if (!ids.length) return "";
  return ids.map((id) => opts?.find((o) => o.id === id)?.name ?? `#${id}`).join(", ");
}

function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card min-w-0 p-4">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

function Feedback({ error, ok }: { error?: unknown; ok?: string | null }) {
  if (error) return <p role="alert" className="text-sm text-bad">{(error as Error).message}</p>;
  if (ok) return <p className="text-sm text-good">{ok}</p>;
  return null;
}

function CheckList({ options, value, onChange, empty }: { options: FilterOption[]; value: number[]; onChange: (v: number[]) => void; empty: string }) {
  if (!options.length) return <p className="text-xs text-muted">{empty}</p>;
  return (
    <div className="grid max-h-48 grid-cols-1 gap-1 overflow-y-auto rounded-lg border border-line p-2 sm:grid-cols-2">
      {options.map((o) => (
        <label key={o.id} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={value.includes(o.id)}
            onChange={(e) => onChange(e.target.checked ? [...value, o.id] : value.filter((x) => x !== o.id))}
          />
          <span className="truncate">{o.name}</span>
        </label>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ usuarios
const EMPTY_USER: AdminUser = { email: "", name: "", role: null, status: "activo", branch_ids: [], warehouse_ids: [], created_at: "", last_login: null };

function UserEditor({ initial, isNew, roles, domain, onDone }: { initial: AdminUser; isNew: boolean; roles: AdminRole[]; domain: string; onDone: () => void }) {
  const qc = useQueryClient();
  const { data: opts } = useFilterOptions();
  const [u, setU] = useState<AdminUser>(initial);
  useEffect(() => setU(initial), [initial]);
  const role = roles.find((r) => r.name === u.role);
  const save = useMutation({
    mutationFn: () => api.admin.saveUser({ ...u, email: u.email.trim().toLowerCase() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-users"] });
      onDone();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.admin.deleteUser(u.email),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-users"] });
      onDone();
    },
  });
  const set = (patch: Partial<AdminUser>) => setU((x) => ({ ...x, ...patch }));

  return (
    <Section title={isNew ? "Nuevo usuario" : `Editar ${u.email}`}>
      <div className="grid gap-3 md:grid-cols-2">
        <label className="space-y-1">
          <span className="text-xs text-ink-2">Correo de Google</span>
          <input className="input w-full" value={u.email} disabled={!isNew} placeholder={`nombre@${domain}`} onChange={(e) => set({ email: e.target.value })} />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-ink-2">Nombre</span>
          <input className="input w-full" value={u.name} onChange={(e) => set({ name: e.target.value })} />
        </label>
        <label className="space-y-1">
          <span className="text-xs text-ink-2">Rol</span>
          <select className="input w-full" value={u.role ?? ""} onChange={(e) => set({ role: e.target.value || null })}>
            <option value="">— Sin rol —</option>
            {roles.map((r) => (
              <option key={r.name} value={r.name}>{r.label}</option>
            ))}
          </select>
          {role && (
            <span className="block text-xs text-muted">
              Ve: {role.admin ? "todo + administración" : role.modules.map((m) => MODULE_TITLES[m]).join(", ") || "nada"}
              {role.ai || role.admin ? " · con IA" : " · sin IA"}
            </span>
          )}
        </label>
        <label className="space-y-1">
          <span className="text-xs text-ink-2">Estado</span>
          <select className="input w-full" value={u.status} onChange={(e) => set({ status: e.target.value as UserStatus })}>
            {(Object.keys(STATUS_LABEL) as UserStatus[]).map((s) => (
              <option key={s} value={s}>{STATUS_LABEL[s]}</option>
            ))}
          </select>
        </label>
        {!role?.admin && (
          <>
            <div className="space-y-1">
              <span className="text-xs text-ink-2">Sucursales permitidas (vacío = todas)</span>
              <CheckList options={opts?.branches ?? []} value={u.branch_ids} onChange={(v) => set({ branch_ids: v })} empty="No hay sucursales en Odoo." />
            </div>
            <div className="space-y-1">
              <span className="text-xs text-ink-2">Almacenes permitidos (vacío = todos)</span>
              <CheckList options={opts?.warehouses ?? []} value={u.warehouse_ids} onChange={(v) => set({ warehouse_ids: v })} empty="No hay almacenes." />
            </div>
            <p className="text-xs text-muted md:col-span-2">
              Con sucursales o almacenes asignados, el usuario solo ve esa información. Las secciones que Odoo no permite filtrar por esa
              dimensión (p. ej. inventario por sucursal) se ocultan en lugar de mostrar datos de toda la empresa. Para gerentes de
              sucursal, asigna la sucursal <strong>y</strong> sus almacenes.
            </p>
          </>
        )}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button className="btn-primary" disabled={save.isPending || !u.email} onClick={() => save.mutate()}>
          {save.isPending ? "Guardando…" : "Guardar"}
        </button>
        <button className="btn-ghost border border-line" onClick={onDone}>Cancelar</button>
        {!isNew && (
          <button
            className="btn-ghost ml-auto text-bad"
            disabled={remove.isPending}
            onClick={() => window.confirm(`¿Eliminar a ${u.email}? Perderá el acceso de inmediato.`) && remove.mutate()}
          >
            Eliminar
          </button>
        )}
      </div>
      <div className="mt-2">
        <Feedback error={save.error || remove.error} />
      </div>
    </Section>
  );
}

function UsersTab() {
  const { data, isLoading, error } = useQuery({ queryKey: ["admin-users"], queryFn: api.admin.users });
  const { data: rolesData } = useQuery({ queryKey: ["admin-roles"], queryFn: api.admin.roles });
  const { data: opts } = useFilterOptions();
  const [editing, setEditing] = useState<{ user: AdminUser; isNew: boolean } | null>(null);
  const [q, setQ] = useState("");
  const roles = rolesData?.roles ?? [];
  const users = useMemo(
    () => (data?.users ?? []).filter((u) => !q || `${u.email} ${u.name}`.toLowerCase().includes(q.toLowerCase())),
    [data, q],
  );
  const pending = (data?.users ?? []).filter((u) => u.status === "pendiente").length;

  if (error) return <ErrorState message={(error as Error).message} />;
  return (
    <div className="space-y-4">
      {editing && (
        <UserEditor initial={editing.user} isNew={editing.isNew} roles={roles} domain={data?.domain ?? ""} onDone={() => setEditing(null)} />
      )}
      <Section
        title={`Usuarios${pending ? ` · ${pending} pendiente(s) de aprobación` : ""}`}
        actions={
          <div className="flex gap-2">
            <input className="input w-48" placeholder="Buscar…" value={q} onChange={(e) => setQ(e.target.value)} />
            <button className="btn-primary" onClick={() => setEditing({ user: { ...EMPTY_USER }, isNew: true })}>Nuevo usuario</button>
          </div>
        }
      >
        <p className="mb-3 text-xs text-muted">
          Solo cuentas de Google @{data?.domain}. Quien entra por primera vez queda «Pendiente» hasta que le asignes rol y lo actives.
          {data?.admin_emails.length ? ` Administradores fijos (.env): ${data.admin_emails.join(", ")}.` : ""}
        </p>
        {isLoading ? (
          <div className="skeleton h-32 w-full" />
        ) : (
          <div className="-mx-4 overflow-x-auto px-4">
            <table className="w-full min-w-max text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-ink-2">
                  <th className="px-2 py-2 font-medium">Usuario</th>
                  <th className="px-2 py-2 font-medium">Rol</th>
                  <th className="px-2 py-2 font-medium">Estado</th>
                  <th className="px-2 py-2 font-medium">Alcance</th>
                  <th className="px-2 py-2 font-medium">Último acceso</th>
                  <th className="px-2 py-2" />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.email} className="border-b border-line/60 last:border-0">
                    <td className="px-2 py-1.5">
                      <div className="font-medium">{u.name || u.email}</div>
                      <div className="text-xs text-muted">{u.email}</div>
                    </td>
                    <td className="px-2 py-1.5">{roles.find((r) => r.name === u.role)?.label ?? <span className="text-muted">—</span>}</td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[u.status]}`}>{STATUS_LABEL[u.status]}</span>
                    </td>
                    <td className="max-w-[16rem] truncate px-2 py-1.5 text-xs text-ink-2">
                      {[names(u.branch_ids, opts?.branches), names(u.warehouse_ids, opts?.warehouses)].filter(Boolean).join(" · ") || "Toda la empresa"}
                    </td>
                    <td className="px-2 py-1.5 text-xs text-ink-2">{fmtDate(u.last_login)}</td>
                    <td className="px-2 py-1.5 text-right">
                      <button className="btn-ghost px-2 py-1 text-xs" onClick={() => setEditing({ user: u, isNew: false })}>
                        {u.status === "pendiente" ? "Aprobar…" : "Editar"}
                      </button>
                    </td>
                  </tr>
                ))}
                {!users.length && (
                  <tr>
                    <td colSpan={6} className="px-2 py-6 text-center text-muted">Sin usuarios todavía.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}

// ------------------------------------------------------------------ roles
function RoleRow({ role, modules, isNew, onSaved }: { role: AdminRole; modules: ModuleName[]; isNew?: boolean; onSaved?: () => void }) {
  const qc = useQueryClient();
  const [r, setR] = useState(role);
  useEffect(() => setR(role), [role]);
  const dirty = JSON.stringify(r) !== JSON.stringify(role) || isNew;
  const save = useMutation({
    mutationFn: () => api.admin.saveRole(r),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-roles"] });
      onSaved?.();
    },
  });
  const remove = useMutation({
    mutationFn: () => api.admin.deleteRole(r.name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-roles"] }),
  });
  const locked = r.name === "admin" && !isNew;
  return (
    <tr className="border-b border-line/60 align-top last:border-0">
      <td className="px-2 py-2">
        <input className="input w-40" value={r.label} onChange={(e) => setR({ ...r, label: e.target.value })} />
        {isNew ? (
          <input className="input mt-1 w-40 text-xs" placeholder="clave (ej. cobranza)" value={r.name} onChange={(e) => setR({ ...r, name: e.target.value.toLowerCase() })} />
        ) : (
          <div className="mt-1 text-xs text-muted">{r.name}</div>
        )}
      </td>
      {modules.map((m) => (
        <td key={m} className="px-2 py-2 text-center">
          <input
            type="checkbox"
            aria-label={`${r.label}: ${MODULE_TITLES[m]}`}
            disabled={locked || r.admin}
            checked={r.admin || r.modules.includes(m)}
            onChange={(e) => setR({ ...r, modules: e.target.checked ? [...r.modules, m] : r.modules.filter((x) => x !== m) })}
          />
        </td>
      ))}
      <td className="px-2 py-2 text-center">
        <input type="checkbox" aria-label="Asistente IA" disabled={locked || r.admin} checked={r.admin || r.ai} onChange={(e) => setR({ ...r, ai: e.target.checked })} />
      </td>
      <td className="px-2 py-2 text-center">
        <input type="checkbox" aria-label="Administración" disabled={locked} checked={r.admin} onChange={(e) => setR({ ...r, admin: e.target.checked })} />
      </td>
      <td className="whitespace-nowrap px-2 py-2 text-right">
        <button className="btn-ghost px-2 py-1 text-xs" disabled={!dirty || save.isPending || !r.name} onClick={() => save.mutate()}>
          Guardar
        </button>
        {!isNew && !locked && (
          <button className="btn-ghost px-2 py-1 text-xs text-bad" onClick={() => window.confirm(`¿Eliminar el rol ${r.label}?`) && remove.mutate()}>
            Eliminar
          </button>
        )}
        <Feedback error={save.error || remove.error} />
      </td>
    </tr>
  );
}

function RolesTab() {
  const { data, error, isLoading } = useQuery({ queryKey: ["admin-roles"], queryFn: api.admin.roles });
  const [adding, setAdding] = useState(false);
  if (error) return <ErrorState message={(error as Error).message} />;
  const modules = data?.modules ?? [];
  return (
    <Section
      title="Roles: qué módulos ve cada rol"
      actions={!adding && <button className="btn-primary" onClick={() => setAdding(true)}>Nuevo rol</button>}
    >
      <p className="mb-3 text-xs text-muted">
        Marca los módulos visibles, el acceso al asistente de IA y si el rol administra el sistema. Los cambios aplican de inmediato a
        todas las sesiones abiertas. El rol «admin» siempre ve todo.
      </p>
      {isLoading ? (
        <div className="skeleton h-32 w-full" />
      ) : (
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-xs text-ink-2">
                <th className="px-2 py-2 text-left font-medium">Rol</th>
                {modules.map((m) => (
                  <th key={m} className="px-2 py-2 font-medium">{MODULE_TITLES[m]}</th>
                ))}
                <th className="px-2 py-2 font-medium">Asistente IA</th>
                <th className="px-2 py-2 font-medium">Administración</th>
                <th className="px-2 py-2" />
              </tr>
            </thead>
            <tbody>
              {data?.roles.map((r) => <RoleRow key={r.name} role={r} modules={modules} />)}
              {adding && (
                <RoleRow isNew role={{ name: "", label: "Nuevo rol", modules: [], ai: false, admin: false }} modules={modules} onSaved={() => setAdding(false)} />
              )}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ configuración
const WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"];
type FieldDef = { key: string; label: string; type: "bool" | "int" | "float" | "text" | "weekday" | "role"; hint?: string };
const GROUPS: { title: string; fields: FieldDef[] }[] = [
  {
    title: "Inteligencia artificial",
    fields: [
      { key: "AI_ENABLED", label: "IA habilitada", type: "bool", hint: "Apaga el asistente y los análisis para todos." },
      { key: "AI_ANONYMIZE", label: "Anonimizar clientes/proveedores", type: "bool" },
      { key: "AI_RATE_LIMIT_PER_MINUTE", label: "Solicitudes de IA por usuario / minuto", type: "int" },
      { key: "AI_RATE_LIMIT_PER_DAY", label: "Solicitudes de IA por usuario / día", type: "int" },
    ],
  },
  {
    title: "Usuarios nuevos (primer ingreso con Google)",
    fields: [
      { key: "NEW_USER_AUTO_APPROVE", label: "Aprobar automáticamente", type: "bool", hint: "Si está apagado, quedan «Pendiente» hasta que un admin los active." },
      { key: "NEW_USER_DEFAULT_ROLE", label: "Rol inicial", type: "role" },
    ],
  },
  {
    title: "Resumen semanal por correo",
    fields: [
      { key: "SUMMARY_RECIPIENTS", label: "Destinatarios (separados por coma)", type: "text" },
      { key: "SUMMARY_WEEKDAY", label: "Día de envío", type: "weekday" },
      { key: "SUMMARY_HOUR", label: "Hora de envío (0–23)", type: "int" },
    ],
  },
  {
    title: "Alertas",
    fields: [
      { key: "ALERT_UTILIZATION_MIN_PCT", label: "Utilización mínima de flota (%)", type: "float" },
      { key: "ALERT_QUARANTINE_MAX_PCT", label: "Mantenimiento máximo (% de la flota)", type: "float" },
      { key: "ALERT_AR_OVERDUE_MAX_PCT", label: "CxC vencida máxima (%)", type: "float" },
    ],
  },
  {
    title: "Rendimiento",
    fields: [{ key: "KPI_CACHE_TTL_SECONDS", label: "Caché de indicadores (segundos)", type: "int" }],
  },
];
const INFO_LABELS: Record<string, string> = {
  odoo_url: "Odoo", odoo_db: "Base de datos", odoo_user: "Usuario de Odoo", ai_provider: "Proveedor de IA", ai_model: "Modelo de IA",
  ai_key_configured: "Clave de IA configurada", google_login: "Login con Google", allowed_domain: "Dominio permitido",
  password_login: "Login con contraseña (emergencia)", smtp_configured: "Correo (SMTP) configurado", prefetch_minutes: "Precálculo (min)",
};

function ConfigTab() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({ queryKey: ["admin-settings"], queryFn: api.admin.settings });
  const { data: rolesData } = useQuery({ queryKey: ["admin-roles"], queryFn: api.admin.roles });
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [ok, setOk] = useState<string | null>(null);
  useEffect(() => setDraft({}), [data]);
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.admin.saveSettings(values),
    onSuccess: (d) => {
      qc.setQueryData(["admin-settings"], d);
      qc.invalidateQueries({ queryKey: ["kpis"] });
      setOk("Configuración guardada.");
    },
  });
  const send = useMutation({ mutationFn: api.admin.sendSummary, onSuccess: (r) => setOk(`Resumen enviado a ${r.sent_to.join(", ")}.`) });

  if (error) return <ErrorState message={(error as Error).message} />;
  if (isLoading || !data) return <div className="skeleton h-64 w-full" />;
  const value = (k: string) => (k in draft ? draft[k] : data.values[k]);
  const set = (k: string, v: unknown) => {
    setOk(null);
    setDraft((d) => ({ ...d, [k]: v }));
  };

  const input = (f: FieldDef) => {
    const v = value(f.key);
    if (f.type === "bool")
      return <input type="checkbox" checked={Boolean(v)} onChange={(e) => set(f.key, e.target.checked)} className="h-4 w-4" />;
    if (f.type === "weekday")
      return (
        <select className="input" value={Number(v)} onChange={(e) => set(f.key, Number(e.target.value))}>
          {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
        </select>
      );
    if (f.type === "role")
      return (
        <select className="input" value={String(v)} onChange={(e) => set(f.key, e.target.value)}>
          {rolesData?.roles.filter((r) => !r.admin).map((r) => <option key={r.name} value={r.name}>{r.label}</option>)}
        </select>
      );
    return (
      <input
        className={`input ${f.type === "text" ? "w-full md:w-96" : "w-32"}`}
        type={f.type === "text" ? "text" : "number"}
        step={f.type === "float" ? "0.1" : "1"}
        value={String(v ?? "")}
        onChange={(e) => set(f.key, f.type === "text" ? e.target.value : e.target.value === "" ? "" : Number(e.target.value))}
      />
    );
  };

  return (
    <div className="space-y-4">
      {GROUPS.map((g) => (
        <Section key={g.title} title={g.title}>
          <div className="space-y-3">
            {g.fields.map((f) => (
              <div key={f.key} className="flex flex-wrap items-center gap-x-4 gap-y-1">
                <label className="w-64 text-sm">{f.label}</label>
                {input(f)}
                {data.overridden.includes(f.key) && (
                  <button className="btn-ghost px-2 py-1 text-xs" title="Volver al valor del .env" onClick={() => save.mutate({ [f.key]: null })}>
                    Restablecer ({String(data.env_defaults[f.key])})
                  </button>
                )}
                {f.hint && <span className="w-full text-xs text-muted md:ml-[17rem]">{f.hint}</span>}
              </div>
            ))}
          </div>
        </Section>
      ))}
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn-primary" disabled={!Object.keys(draft).length || save.isPending} onClick={() => save.mutate(draft)}>
          {save.isPending ? "Guardando…" : "Guardar cambios"}
        </button>
        <button className="btn-ghost border border-line" disabled={send.isPending || !data.info.smtp_configured} onClick={() => send.mutate()}
          title={data.info.smtp_configured ? "" : "Configura SMTP en el .env"}>
          {send.isPending ? "Enviando…" : "Enviar resumen de prueba"}
        </button>
        <a className="btn-ghost border border-line" href="/api/admin/summary/preview" onClick={(e) => { e.preventDefault(); openPreview(); }}>
          Vista previa del resumen
        </a>
        <Feedback error={save.error || send.error} ok={ok} />
      </div>
      <Section title="Conexiones (solo lectura: se cambian en el archivo .env del servidor)">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          {Object.entries(data.info).map(([k, v]) => (
            <div key={k} className="flex justify-between gap-3 border-b border-line/60 py-1">
              <dt className="text-ink-2">{INFO_LABELS[k] ?? k}</dt>
              <dd className="truncate font-medium">{typeof v === "boolean" ? (v ? "Sí" : "No") : String(v ?? "—")}</dd>
            </div>
          ))}
        </dl>
      </Section>
    </div>
  );
}

/** La vista previa requiere el token: se descarga con fetch y se abre como documento local. */
async function openPreview() {
  const w = window.open("", "_blank");
  try {
    const { apiFetch } = await import("../api/client");
    const html = await (await apiFetch("/api/admin/summary/preview")).text();
    if (w) {
      w.document.open();
      w.document.write(html);
      w.document.close();
    }
  } catch (e) {
    w?.close();
    window.alert((e as Error).message);
  }
}

// ------------------------------------------------------------------ bitácora
const EVENT_LABEL: Record<string, string> = {
  login: "Inicio de sesión", login_rechazado: "Intento rechazado", login_denegado: "Acceso denegado", registro: "Primer ingreso",
  usuario_creado: "Usuario creado", usuario_actualizado: "Usuario modificado", usuario_eliminado: "Usuario eliminado",
  rol_actualizado: "Rol modificado", rol_eliminado: "Rol eliminado", configuracion: "Configuración modificada",
};

function AccessTab() {
  const { data, error, isLoading, refetch, isFetching } = useQuery({ queryKey: ["admin-access"], queryFn: () => api.admin.accessLog(500) });
  const [q, setQ] = useState("");
  if (error) return <ErrorState message={(error as Error).message} onRetry={() => refetch()} />;
  const events = (data?.events ?? []).filter((e) => !q || `${e.email} ${e.event} ${e.detail} ${e.ip}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <Section
      title="Bitácora de accesos y cambios (últimos 500)"
      actions={
        <div className="flex gap-2">
          <input className="input w-48" placeholder="Filtrar…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button className="btn-ghost border border-line" onClick={() => refetch()} disabled={isFetching}>Actualizar</button>
        </div>
      }
    >
      {isLoading ? (
        <div className="skeleton h-32 w-full" />
      ) : (
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="w-full min-w-max text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                <th className="px-2 py-2 font-medium">Fecha</th>
                <th className="px-2 py-2 font-medium">Usuario</th>
                <th className="px-2 py-2 font-medium">Evento</th>
                <th className="px-2 py-2 font-medium">IP</th>
                <th className="px-2 py-2 font-medium">Detalle</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i} className="border-b border-line/60 last:border-0">
                  <td className="whitespace-nowrap px-2 py-1.5 text-xs">{fmtDate(e.ts)}</td>
                  <td className="px-2 py-1.5">{e.email}</td>
                  <td className={`px-2 py-1.5 ${e.event.includes("rechazado") || e.event.includes("denegado") ? "text-bad" : ""}`}>
                    {EVENT_LABEL[e.event] ?? e.event}
                  </td>
                  <td className="px-2 py-1.5 text-xs text-ink-2">{e.ip ?? ""}</td>
                  <td className="max-w-[28rem] truncate px-2 py-1.5 text-xs text-ink-2" title={e.detail ?? ""}>{e.detail}</td>
                </tr>
              ))}
              {!events.length && (
                <tr>
                  <td colSpan={5} className="px-2 py-6 text-center text-muted">Sin eventos.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("usuarios");
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Administración</h1>
        <p className="text-xs text-muted">Sesión: {user?.username}. Los cambios aplican de inmediato.</p>
      </div>
      <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${tab === t.key ? "border-brand font-medium text-brand" : "border-transparent text-ink-2 hover:text-ink"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "usuarios" && <UsersTab />}
      {tab === "roles" && <RolesTab />}
      {tab === "config" && <ConfigTab />}
      {tab === "accesos" && <AccessTab />}
    </div>
  );
}
