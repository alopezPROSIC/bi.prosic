"""Mapeo de modelos y campos de Odoo usados por los KPIs.

Ajusta ESTE archivo (no la lógica) si en la instancia de PROSIC un campo se llama
distinto, no existe o hay un campo personalizado mejor. Usa
``GET /api/odoo/fields/{modelo}`` para descubrir los nombres reales.

Validado contra el esquema exportado (Odoo 15 Enterprise + módulos de PROSIC):
- Lotes/series: ``stock.production.lot`` (en Odoo 16+ sería ``stock.lot``).
- Rentas: módulo de terceros (``rental.order.contract``, ``sale.order.is_rental_so``,
  ``stock.warehouse.rental_out_location_id``), NO el ``sale_renting`` de Odoo.
- Sucursal: campo personalizado ``branch_office_id`` (account.analytic.branch.office)
  presente en sale.order, account.move, purchase.order y maintenance.request.

Convenciones de ``ModelMap``:
- ``fields``: nombre lógico -> nombre real en Odoo. Pon ``None`` para desactivar.
- ``branch_field`` / ``warehouse_field`` / ``company_field``: campo para filtrar; puede
  ser una ruta con punto (``order_id.branch_office_id``). ``None`` = el filtro no
  aplica a ese modelo (el KPI lo indica en ``notes``).
- ``warehouse_match``: cómo se compara el almacén:
    "id"                  -> campo = warehouse.id
    "view_location"       -> campo child_of warehouse.view_location_id
    "rental_out_location" -> campo = warehouse.rental_out_location_id
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

WarehouseMatch = Literal["id", "view_location", "rental_out_location"]


@dataclass(frozen=True)
class ModelMap:
    model: str
    fields: dict[str, str | None]
    date_field: str | None = None
    company_field: str | None = "company_id"
    branch_field: str | None = None
    warehouse_field: str | None = None
    warehouse_match: WarehouseMatch = "id"
    base_domain: list[Any] = field(default_factory=list)

    def f(self, key: str) -> str:
        value = self.fields.get(key)
        if not value:
            raise KeyError(f"{self.model}: el campo lógico '{key}' no está mapeado")
        return value

    def has(self, key: str) -> bool:
        return bool(self.fields.get(key))

    def physical_fields(self) -> list[str]:
        """Campos (primer segmento) que deben existir para usar este mapeo."""
        names = [v for v in self.fields.values() if v]
        names += [x for x in (self.date_field, self.company_field, self.branch_field, self.warehouse_field) if x]
        return sorted({n.split(".")[0] for n in names})


# ----------------------------------------------------------------- catálogos
COMPANY = ModelMap(model="res.company", fields={"name": "name"}, company_field=None)
WAREHOUSE = ModelMap(
    model="stock.warehouse",
    fields={
        "name": "name",
        "code": "code",
        "view_location": "view_location_id",
        "lot_stock": "lot_stock_id",
        "rental_out_location": "rental_out_location_id",  # personalizado (módulo de rentas)
    },
)
# Modelo de "Sucursal" contable/analítica de PROSIC. Pon None si no se usa.
BRANCH_MODEL: str | None = "account.analytic.branch.office"

# ------------------------------------------------------------------- VENTAS
# sale.report ya convierte montos a la moneda de la compañía (MXN).
SALE_REPORT = ModelMap(
    model="sale.report",
    date_field="date",
    branch_field="order_id.branch_office_id",
    warehouse_field="warehouse_id",
    fields={
        "amount": "price_subtotal",  # subtotal sin IVA
        "partner": "partner_id",
        "salesperson": "user_id",
        "category": "categ_id",
        "product": "product_id",
        "order": "order_id",
        "state": "state",
    },
    base_domain=[("state", "in", ["sale", "done"])],
)
SALE_ORDER = ModelMap(
    model="sale.order",
    date_field="date_order",
    branch_field="branch_office_id",
    warehouse_field="warehouse_id",
    fields={"state": "state", "is_rental": "is_rental_so", "amount": "amount_untaxed"},
    base_domain=[("state", "in", ["sale", "done"])],
)

# ------------------------------------------------------------- FACTURACIÓN
# Se usan campos *_signed: moneda de la compañía y notas de crédito en negativo.
CUSTOMER_INVOICE = ModelMap(
    model="account.move",
    date_field="invoice_date",
    branch_field="branch_office_id",
    warehouse_field=None,
    fields={
        "move_type": "move_type",
        "state": "state",
        "total": "amount_total_signed",
        "untaxed": "amount_untaxed_signed",
        "residual": "amount_residual_signed",
        "due_date": "invoice_date_due",
        "partner": "commercial_partner_id",
        "payment_state": "payment_state",
    },
    base_domain=[("state", "=", "posted")],
)
# Cobranza: se suma el ASIENTO del pago (account.move con payment_id). En Odoo 15 los montos en moneda
# de la compañía de account.payment (amount_company_currency_signed) no están almacenados y read_group
# no puede sumarlos; amount_total_signed del asiento sí (moneda de la compañía, positivo).
CUSTOMER_PAYMENT = ModelMap(
    model="account.move",
    date_field="date",
    branch_field=None,  # los asientos de pago no traen sucursal
    fields={"amount": "amount_total_signed"},
    base_domain=[
        ("state", "=", "posted"),
        ("payment_id", "!=", False),
        ("payment_id.payment_type", "=", "inbound"),
        ("payment_id.partner_type", "=", "customer"),
        ("payment_id.is_internal_transfer", "=", False),
    ],
)

# ------------------------------------------------------------------- RENTAS
RENTAL_ORDER_FILTER = [("order_id.is_rental_so", "=", True)]  # aplicado sobre sale.report
RENTAL_CONTRACT = ModelMap(
    model="rental.order.contract",
    date_field="rental_start_time",
    branch_field="order_id.branch_office_id",
    warehouse_field="location_dest_id",
    warehouse_match="rental_out_location",
    fields={
        "start": "rental_start_time",
        "end": "rental_end_time",
        "closed": "closed_time",
        "qty": "rental_qty",
        "product": "product_id",
        "order": "order_id",
        "partner": "partner_id",
        "state": "state",
        "price": "price_subtotal",  # importe del contrato sin IVA (se prorratea por días dentro del periodo)
        "location_dest": "location_dest_id",
    },
)
# Estados reales (Odoo PROSIC): new=Draft, ready, in_progress, expired, closed, cancel.
# Para utilización del periodo NO cuentan los borradores ni cancelados (los cerrados sí: fueron rentas reales;
# su closed_time recorta el fin).
RENTAL_CONTRACT_EXCLUDED_STATES = ["new", "cancel"]
# "Rentas por vencer": solo contratos con el equipo actualmente en renta.
RENTAL_CONTRACT_ACTIVE_STATES = ["in_progress"]

STOCK_QUANT = ModelMap(
    model="stock.quant",
    warehouse_field="location_id",
    warehouse_match="view_location",
    fields={"quantity": "quantity", "location": "location_id", "product": "product_id", "lot": "lot_id"},
)
# Movimientos realizados: fecha del último ingreso de cada serie a su ubicación actual
# (stock.quant.in_date NO sirve: Odoo conserva la fecha más antigua).
MOVE_LINE = ModelMap(
    model="stock.move.line",
    fields={"lot": "lot_id", "location_dest": "location_dest_id", "date": "date", "state": "state"},
    base_domain=[("state", "=", "done")],
)
# Ubicaciones internas de equipo que regresó de renta y aún no se inspecciona (no se puede rentar todavía).
# En PROSIC «<ALM>/Rent In» es el stock DISPONIBLE para renta (ahí están las ~520 unidades disponibles),
# así que la lista va vacía y el estatus «En retorno» no se muestra. Si se crea una ubicación de
# inspección (p. ej. «Inspección»), agrégala aquí.
RETURN_LOCATION_KEYWORDS: list[str] = []
# Días sin moverse a partir de los cuales un equipo disponible se considera ocioso.
IDLE_DAYS = 30
LOCATION = ModelMap(
    model="stock.location",
    fields={"name": "complete_name", "usage": "usage", "parent_path": "parent_path"},
)
# Dominio sobre stock.quant que identifica equipos rentables.
RENTAL_PRODUCT_DOMAIN = [("product_id.rental_ok", "=", True)]
# Ubicaciones internas cuyo nombre completo contenga alguna de estas palabras se
# consideran "en mantenimiento". En PROSIC no hay ubicaciones "Taller"/"Mantenimiento"; cada almacén
# tiene "<ALM>/Cuarentena", que se asume como equipo fuera de servicio (valídalo con operaciones).
MAINTENANCE_LOCATION_KEYWORDS = ["Cuarentena", "Mantenimiento", "Taller", "Reparación", "Reparacion"]
PRODUCT = ModelMap(
    model="product.product",
    # Cambia "categ_id" por "rental_categ_id" para agrupar por la categoría de rentas.
    fields={"category": "categ_id", "name": "display_name"},
)

# ------------------------------------------------------------- MANTENIMIENTO
MAINT_REQUEST = ModelMap(
    model="maintenance.request",
    date_field="request_date",
    branch_field="branch_office_id",
    warehouse_field=None,
    fields={
        "stage": "stage_id",
        "type": "maintenance_type",  # preventive | corrective
        "equipment": "equipment_id",
        "request_date": "request_date",
        "close_date": "close_date",
        # Campo personalizado de costo en la petición (p. ej. "x_studio_costo_total").
        # Si se define, el costo se toma de aquí en lugar de repair.order.
        "cost": None,
    },
)
MAINT_OPEN_DOMAIN = [("stage_id.done", "=", False)]
MAINT_CLOSED_DOMAIN = [("stage_id.done", "=", True)]
# Etapas canceladas (campo personalizado "cancel" en maintenance.stage).
MAINT_EXCLUDE_DOMAIN = [("stage_id.cancel", "=", False)]

REPAIR_ORDER = ModelMap(
    model="repair.order",
    date_field="create_date",
    branch_field=None,
    # equipment_id no está almacenado en repair.order (no se puede agrupar): se usa el número de serie.
    fields={"amount": "amount_untaxed", "equipment": "lot_id", "product": "product_id", "state": "state"},
    base_domain=[("state", "!=", "cancel")],
)

# ------------------------------------------------------------------ COMPRAS
# purchase.report convierte a moneda de la compañía.
PURCHASE_REPORT = ModelMap(
    model="purchase.report",
    date_field="date_approve",
    branch_field="order_id.branch_office_id",
    # En Odoo 15, purchase.report.picking_type_id ya es el ALMACÉN (relación stock.warehouse, etiqueta «Warehouse»).
    warehouse_field="picking_type_id",
    fields={
        "amount": "untaxed_total",
        "partner": "partner_id",
        "order": "order_id",
        "qty_ordered": "qty_ordered",
        "qty_received": "qty_received",
        "qty_to_bill": "qty_to_be_billed",
        "state": "state",
    },
    base_domain=[("state", "in", ["purchase", "done"])],
)
# Días hacia atrás para buscar órdenes pendientes de recibir/facturar.
PURCHASE_PENDING_LOOKBACK_DAYS = 365

VENDOR_BILL = ModelMap(
    model="account.move",
    date_field="invoice_date",
    branch_field="branch_office_id",
    fields={
        "move_type": "move_type",
        "total": "amount_total_signed",
        "residual": "amount_residual_signed",
        "due_date": "invoice_date_due",
        "partner": "commercial_partner_id",
    },
    base_domain=[("state", "=", "posted")],
)
