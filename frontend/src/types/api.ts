// Tipos espejo de los modelos Pydantic del backend (app/schemas).

export type ModuleName = "ventas" | "comisiones" | "rentas" | "historico" | "mantenimiento" | "compras" | "pronostico";
export type Unit = "MXN" | "%" | "count" | "days";
export type Entity = "cliente" | "proveedor" | "vendedor";
export type CompareMode = "previous" | "year" | "none";

export interface KpiFilters {
  date_from: string;
  date_to: string;
  company_id?: number | null;
  warehouse_id?: number | null;
  branch_id?: number | null;
  compare: CompareMode;
}

export interface Period {
  date_from: string;
  date_to: string;
}

export interface KpiCard {
  key: string;
  label: string;
  value: number | null;
  previous: number | null;
  delta_pct: number | null;
  unit: Unit;
  higher_is_better: boolean;
  hint: string | null;
}

export interface ChartSeries {
  key: string;
  label: string;
}

export type ChartType = "line" | "bar" | "hbar" | "donut" | "stacked_bar";

export interface Chart {
  key: string;
  title: string;
  type: ChartType;
  unit: Unit;
  x_key: string;
  series: ChartSeries[];
  data: Record<string, string | number | null>[];
  entity: Entity | null;
  /** Drill-down: clic en una barra aplica este filtro global con el "_id" de la fila. */
  drill?: "warehouse_id" | "branch_id" | null;
}

export type ColumnType = "text" | "money" | "number" | "date" | "percent" | "days";

export interface Column {
  key: string;
  label: string;
  type: ColumnType;
  entity: Entity | null;
}

export interface Table {
  key: string;
  title: string;
  columns: Column[];
  rows: Record<string, string | number | null>[];
  ai_include: boolean;
  /** Si existe, las filas con "_id" enlazan al registro en Odoo. */
  odoo_model?: string | null;
}

export interface ModuleKpis {
  module: ModuleName;
  available: boolean;
  reason: string | null;
  filters: KpiFilters;
  period: Period;
  previous_period: Period | null;
  cards: KpiCard[];
  charts: Chart[];
  tables: Table[];
  notes: string[];
  generated_at: string;
}

export interface FilterOption {
  id: number;
  name: string;
  company_id: number | null;
}

export interface FilterOptions {
  companies: FilterOption[];
  warehouses: FilterOption[];
  branches: FilterOption[];
  odoo_url?: string | null;
  /** Alcance fijo del usuario (almacén/sucursal); esos filtros quedan bloqueados. */
  scope?: { warehouse_id?: number | null; branch_id?: number | null; warehouse_ids?: number[]; branch_ids?: number[] };
}

export type Severity = "info" | "alerta" | "critico";

export interface AiAnalysis {
  resumen: string;
  hallazgos: { titulo: string; detalle: string; severidad: Severity; kpi_relacionado: string }[];
  recomendaciones: { accion: string; impacto_esperado: string; prioridad: "alta" | "media" | "baja" }[];
  preguntas_sugeridas: string[];
  modulo: ModuleName | null;
  cached: boolean;
  model: string | null;
}

export interface User {
  username: string;
  name: string;
  role: string;
  role_label: string;
  is_admin: boolean;
  modules: ModuleName[];
  ai: boolean;
  warehouse_ids: number[];
  branch_ids: number[];
  warehouse_id?: number | null;
  branch_id?: number | null;
}

export interface AuthConfig {
  google_client_id: string | null;
  allowed_domain: string;
  password_login: boolean;
}

// ---------- panel de administración
export type UserStatus = "activo" | "pendiente" | "bloqueado";

export interface AdminUser {
  email: string;
  name: string;
  role: string | null;
  status: UserStatus;
  branch_ids: number[];
  warehouse_ids: number[];
  created_at: string;
  last_login: string | null;
}

export interface AdminRole {
  name: string;
  label: string;
  modules: ModuleName[];
  ai: boolean;
  admin: boolean;
}

export interface AdminSettings {
  values: Record<string, string | number | boolean>;
  overridden: string[];
  env_defaults: Record<string, string | number | boolean>;
  info: Record<string, string | number | boolean | null>;
}

export interface AccessEvent {
  ts: string;
  email: string;
  event: string;
  ip: string | null;
  detail: string | null;
}

export interface TokenResponse {
  access_token: string;
  expires_in: number;
  user: User;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

/** Consulta del histórico de flota (registros guardados en el sistema). Fechas en ISO. */
export interface FleetQuery {
  formato: "diario" | "corte";
  desde: string;
  hasta: string;
  warehouse_id?: number | null;
  estado?: number | null;
  familia?: string;
  subfamilia?: string;
  centro?: string;
  propietario?: string;
  subasta?: "si" | "no" | "";
  q?: string;
}

export interface FleetRecords {
  formato: "diario" | "corte";
  desde: string;
  hasta: string;
  first_day: string;
  last_day: string;
  total: number;
  page: number;
  page_size: number;
  max_export_rows: number;
  columns: Column[];
  rows: Record<string, string | number | null>[];
  options: {
    estados: { id: number; label: string }[];
    familia: string[];
    subfamilia: string[];
    cost_center: string[];
    owner: string[];
  };
}
