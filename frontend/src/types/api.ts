// Tipos espejo de los modelos Pydantic del backend (app/schemas).

export type ModuleName = "ventas" | "comisiones" | "rentas" | "historico" | "mantenimiento" | "compras" | "pronostico";
/** Permisos de un rol: los módulos de indicadores más «datos» (extractor de Odoo). */
export type Permission = ModuleName | "datos";
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
  /** Importes con o sin IVA (Ventas y facturación; cobrado y cartera siempre con IVA). */
  iva?: "sin" | "con";
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
  drill?: "warehouse_id" | "branch_id" | "vendedor" | null;
  /** Eje con orden propio (meses, días, rangos): no se reordena. Las demás se muestran de menor a mayor. */
  ordered?: boolean;
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
  modules: Permission[];
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
  modules: Permission[];
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
  max_xlsx_rows: number;
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

// ------------------------------------------------------------------ comisiones (Fases 02 a 05)
export interface CommissionTramo {
  desde: number;
  valor: number;
}
export interface CommissionPuesto {
  key: string;
  label: string;
  base: "propia" | "sucursal";
  tramos: CommissionTramo[];
  /** Cuentas propias del puesto; null = las generales de la depuración. */
  cuentas?: string[] | null;
}
export interface CommissionVendedor {
  odoo_id: number;
  nombre: string;
  puesto: string;
  sucursal_id: number | null;
  correo: string;
  activo: boolean;
}
export interface CommissionSucursal {
  branch_id: number;
  nombre: string;
  almacenes: number[];
}
export interface CommissionPlan {
  puestos: CommissionPuesto[];
  vendedores: CommissionVendedor[];
  sucursales: CommissionSucursal[];
  ocupacion: { activo: boolean; tramos: CommissionTramo[] };
  depuracion: CommissionDepuracion;
}
export type CommissionRuleAction = "reasignar" | "excluir" | "incluir";
export interface CommissionRule {
  id: string;
  activo: boolean;
  accion: CommissionRuleAction;
  motivo: string;
  vendedor_id: number | null;
  sucursal_id: number | null;
  cliente: string;
  centro_costo: string;
  facturas: string[];
  desde: string | null;
  hasta: string | null;
  destino_vendedor_id: number | null;
  destino_sucursal_id: number | null;
  porcentaje: number;
}
export interface CommissionDepuracion {
  cuentas: string[];
  notas_credito: "prorrateo" | "todas" | "ninguna";
  max_dias: number;
  reglas: CommissionRule[];
}
export interface CommissionAccount {
  code: string;
  nombre: string;
  nota_credito: boolean;
}
export interface CommissionPlanResponse {
  plan: CommissionPlan;
  version: number;
  updated_at: string | null;
  updated_by: string | null;
}
export interface CommissionAjuste {
  odoo_id: number;
  monto: number;
  motivo: string;
}
export interface CommissionMonth {
  metas_vendedor: Record<string, number>;
  metas_sucursal: Record<string, number>;
  manual: Record<string, number>;
  ajustes: CommissionAjuste[];
}
export interface CommissionMonthResponse {
  mes: string;
  datos: CommissionMonth;
  updated_at: string | null;
  updated_by: string | null;
}
export interface CommissionSending {
  smtp: boolean;
  automatico: boolean;
  dia: number;
  copia: string;
  ultimo_envio_automatico: string | null;
  destinatarios: { odoo_id: number; nombre: string; correo: string; gerente: boolean }[];
}

// ------------------------------------------------------------------ ocupación diaria (tacómetro)
export interface OccupancyWarehouse {
  id: number | null;
  nombre: string;
  flota: number;
  ocupadas: number;
  pct: number | null;
}
export interface OccupancyReading {
  ts: string;
  stale?: boolean;
  flota: number;
  ocupadas: number;
  pct: number | null;
  estados: { grupo: string; unidades: number }[];
  almacenes: OccupancyWarehouse[];
}
export interface OccupancyClose extends OccupancyReading {
  dia: string;
}
export interface OccupancyResponse {
  ahora: OccupancyReading | null;
  manana: OccupancyReading | null;
  cierre: OccupancyClose | null;
  tendencia: { dia: string; flota: number; ocupadas: number; pct: number | null }[];
  promedio_mes: number | null;
  meta: number;
  minimo: number;
  estados_ocupados: string[];
  estados_excluidos: string[];
  hora_manana: string;
  hora_cierre: string;
  minutos_vivo: number;
}

// ---------- Datos de Odoo (extractor y sincronización)
export type DataOp = "=" | "!=" | "contiene" | "no_contiene" | ">" | ">=" | "<" | "<=" | "en" | "vacio" | "no_vacio";
export type DataPreset =
  | "mes_actual" | "mes_anterior" | "trimestre_actual" | "anio_actual" | "anio_anterior"
  | "ultimos_30" | "ultimos_90" | "ultimos_365" | "personalizado" | "sin_fecha";

export interface DataQuery {
  id?: string | null;
  nombre: string;
  descripcion: string;
  fuente: string;
  columnas: { campo: string; etiqueta: string }[];
  filtros: { campo: string; op: DataOp; valor: string | number | boolean | null }[];
  periodo: { campo: string; preset: DataPreset; desde?: string | null; hasta?: string | null };
  agrupar: { campos: string[]; sumar: string[] };
  orden: string;
  compartida: boolean;
  // solo lectura (del servidor)
  propietario?: string;
  actualizada?: string;
  actualizada_por?: string;
  editable?: boolean;
}

export interface DataSource {
  key: string;
  label: string;
  descripcion: string;
  modelo: string;
  fechas: string[];
  columnas: string[];
  fecha_opcional: boolean;
  disponible: boolean;
  motivo: string | null;
}

export interface DataField {
  campo: string;
  etiqueta: string;
  tipo: string;
  opciones: [string, string][] | null;
  relacion: string | null;
  personalizado: boolean;
  agrupable: boolean;
}

export interface DataResult {
  columnas: { key: string; label: string; type: "text" | "money" | "number" | "date" | "datetime" }[];
  filas: Record<string, string | number | null>[];
  total: number;
  totales: Record<string, number>;
  vista_previa: boolean;
  agrupado: boolean;
  grupos: number;
  truncado: boolean;
  fuente: string;
  modelo: string;
  periodo: { desde: string; hasta: string } | null;
  generado: string;
  desde_cache: boolean;
}

export interface SyncRun {
  ts: string;
  ok: boolean;
  segundos: number;
  error: string | null;
  tipo: "nocturna" | "manual";
}

export interface SyncView {
  hora: string | null;
  siguiente: string | null;
  modulos: { key: string; label: string; activo: boolean; ultimo: SyncRun | null }[];
  flota: { ultimo: SyncRun | null; ultima_sincronizacion: string | null };
  hora_manana: string | null;
  tope_llamadas_min: number;
  tope_registros: number;
}

// ---------- panel inteligente de comisiones
export type CommissionAlertLevel = "riesgo" | "revisar" | "oportunidad" | "info" | "ok";
export interface CommissionAlert {
  nivel: CommissionAlertLevel;
  titulo: string;
  detalle: string;
  monto: number | null;
  vendedores: (number | null)[];
  accion: "cobranza" | "reglas" | "plan" | "metas" | null;
}
export interface CommissionFunnelStep {
  key: string;
  label: string;
  monto: number;
  tipo: "total" | "resta" | "resultado" | "comision";
  detalle: string;
}
export interface CommissionSellerInvoice {
  factura: string;
  cliente: string;
  fecha_cobro?: string;
  fecha_factura?: string;
  base: number;
  dias?: number | null;
  saldo?: number;
  cobrado_pct?: number;
  limite?: string | null;
  traspaso?: string | null;
  regla?: string | null;
}
export interface CommissionSeller {
  id: number | null;
  nombre: string;
  puesto: string;
  sucursal: string;
  base_tipo: "Propia" | "Sucursal";
  base: number;
  comision: number;
  facturas: number;
  ajustes: number;
  meses: { mes: string; label: string; base: number; comision: number }[];
  observaciones: string[];
  activo: boolean;
  en_plan: boolean;
  pct: number;
  factor: number;
  meta: number | null;
  cumplimiento: number | null;
  base_mes: number;
  comision_mes: number;
  pct_mes: number;
  tramos: { desde: number; valor: number }[];
  siguiente: { desde: number; valor: number; falta_base: number; comision_extra: number } | null;
  potencial_base: number;
  potencial_comision: number;
  riesgo_base: number;
  riesgo_comision: number;
  perdida_base: number;
  facturas_top: CommissionSellerInvoice[];
  abiertas_top: CommissionSellerInvoice[];
  abiertas: number;
}
export interface CommissionPanel {
  meses: { mes: string; label: string }[];
  preliminar: boolean;
  cuentas: string[];
  max_dias: number;
  resumen: {
    comision: number;
    base_comisionable: number;
    vendedores: number;
    potencial_base: number;
    potencial_comision: number;
    riesgo_base: number;
    riesgo_comision: number;
    cartera_disponible: boolean;
  };
  embudo: CommissionFunnelStep[];
  alertas: CommissionAlert[];
  vendedores: CommissionSeller[];
  notas: string[];
}

// ------------------------------------------------------------------ vendedores
export interface SellerRow {
  id: number | null;
  nombre: string;
  facturado: number;
  facturado_anterior: number | null;
  pedidos_monto: number;
  pedidos: number;
  facturas: number;
  rank: number;
  rank_anterior: number | null;
  participacion: number | null;
}

export interface SellerRanking {
  periodo: Period;
  anterior: Period | null;
  iva: "sin" | "con";
  total: number;
  vendedores: SellerRow[];
}

export interface SellerCommission {
  comision: number;
  base: number;
  meta: number | null;
  cumplimiento: number | null;
  pct: number | null;
  puesto: string | null;
  siguiente: { desde: number; valor: number; falta_base: number; comision_extra: number } | null;
  potencial_comision: number;
  potencial_base: number;
  riesgo_comision: number;
  riesgo_base: number;
  facturas: number;
  observaciones: string[] | string | null;
  preliminar: boolean | null;
  meses: { mes: string; label: string }[] | null;
}

export interface SellerProfile {
  id: number;
  nombre: string;
  ranking: SellerRow | null;
  vendedores: number;
  kpis: ModuleKpis;
  comision: SellerCommission | null;
}
