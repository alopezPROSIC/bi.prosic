"""Datos simulados de Odoo para las pruebas."""
from __future__ import annotations

from datetime import date, timedelta

TODAY = date.today()


def d(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def dt(days_ago: int) -> str:
    return f"{d(days_ago)} 18:00:00"


def base_data() -> dict:
    return {
        "res.company": [{"id": 1, "name": "PROSIC", "sequence": 1}],
        "stock.warehouse": [
            {"id": 1, "name": "Monterrey", "company_id": [1, "PROSIC"], "view_location_id": [10, "MTY"],
             "lot_stock_id": [11, "MTY/Stock"], "rental_out_location_id": [50, "Renta MTY"], "sequence": 1},
            {"id": 2, "name": "Saltillo", "company_id": [1, "PROSIC"], "view_location_id": [20, "SAL"],
             "lot_stock_id": [21, "SAL/Stock"], "rental_out_location_id": [60, "Renta SAL"], "sequence": 2},
        ],
        "account.analytic.branch.office": [{"id": 7, "name": "Sucursal Norte"}],
        "sale.order": [
            {"id": 1, "state": "sale", "date_order": dt(5), "branch_office_id": [7, "Sucursal Norte"],
             "warehouse_id": [1, "Monterrey"], "company_id": [1, "PROSIC"], "is_rental_so": True, "amount_untaxed": 1000},
            {"id": 2, "state": "sale", "date_order": dt(6), "branch_office_id": False,
             "warehouse_id": [2, "Saltillo"], "company_id": [1, "PROSIC"], "is_rental_so": False, "amount_untaxed": 3000},
            {"id": 3, "state": "cancel", "date_order": dt(6), "branch_office_id": False,
             "warehouse_id": [2, "Saltillo"], "company_id": [1, "PROSIC"], "is_rental_so": False, "amount_untaxed": 999},
        ],
        "sale.report": [
            {"id": 1, "date": dt(5), "state": "sale", "price_subtotal": 1000.0, "partner_id": [100, "Constructora Alfa SA"],
             "user_id": [5, "Juan Vendedor"], "categ_id": [3, "Tijeras"], "product_id": [30, "Tijera 19ft"],
             "order_id": [1, "S001"], "warehouse_id": [1, "Monterrey"], "company_id": [1, "PROSIC"]},
            {"id": 2, "date": dt(6), "state": "sale", "price_subtotal": 3000.0, "partner_id": [101, "Grupo Beta"],
             "user_id": [6, "Ana Ventas"], "categ_id": [4, "Brazos"], "product_id": [31, "Brazo 45ft"],
             "order_id": [2, "S002"], "warehouse_id": [2, "Saltillo"], "company_id": [1, "PROSIC"]},
            {"id": 3, "date": dt(6), "state": "cancel", "price_subtotal": 999.0, "partner_id": [101, "Grupo Beta"],
             "user_id": [6, "Ana Ventas"], "categ_id": [4, "Brazos"], "product_id": [31, "Brazo 45ft"],
             "order_id": [3, "S003"], "warehouse_id": [2, "Saltillo"], "company_id": [1, "PROSIC"]},
        ],
        "account.move": [
            # factura vencida hace 45 días con saldo
            {"id": 1, "move_type": "out_invoice", "state": "posted", "invoice_date": d(80), "invoice_date_due": d(45),
             "amount_untaxed_signed": 1000, "amount_total_signed": 1160, "amount_residual_signed": 1160,
             "commercial_partner_id": [100, "Constructora Alfa SA"], "payment_state": "not_paid",
             "branch_office_id": [7, "Sucursal Norte"], "company_id": [1, "PROSIC"]},
            # factura por vencer
            {"id": 2, "move_type": "out_invoice", "state": "posted", "invoice_date": d(5), "invoice_date_due": d(-25),
             "amount_untaxed_signed": 3000, "amount_total_signed": 3480, "amount_residual_signed": 3480,
             "commercial_partner_id": [101, "Grupo Beta"], "payment_state": "not_paid",
             "branch_office_id": False, "company_id": [1, "PROSIC"]},
            # nota de crédito
            {"id": 3, "move_type": "out_refund", "state": "posted", "invoice_date": d(4), "invoice_date_due": d(4),
             "amount_untaxed_signed": -200, "amount_total_signed": -232, "amount_residual_signed": 0,
             "commercial_partner_id": [101, "Grupo Beta"], "payment_state": "paid",
             "branch_office_id": False, "company_id": [1, "PROSIC"]},
            # factura de proveedor vencida 100 días
            {"id": 4, "move_type": "in_invoice", "state": "posted", "invoice_date": d(130), "invoice_date_due": d(100),
             "amount_untaxed_signed": -500, "amount_total_signed": -580, "amount_residual_signed": -580,
             "commercial_partner_id": [200, "Refacciones Gama"], "payment_state": "not_paid",
             "branch_office_id": False, "company_id": [1, "PROSIC"]},
            # asiento de un pago de cliente (la cobranza se suma desde aquí)
            {"id": 5, "move_type": "entry", "state": "posted", "date": d(3), "payment_id": [1, "PBNK1/0001"],
             "amount_total_signed": 500, "company_id": [1, "PROSIC"]},
        ],
        "account.payment": [
            {"id": 1, "state": "posted", "payment_type": "inbound", "partner_type": "customer",
             "is_internal_transfer": False, "amount": 500, "company_id": [1, "PROSIC"]},
        ],
        "product.product": [
            {"id": 30, "categ_id": [3, "Tijeras"], "display_name": "Tijera 19ft", "rental_ok": True},
            {"id": 31, "categ_id": [4, "Brazos"], "display_name": "Brazo 45ft", "rental_ok": True},
        ],
        "stock.location": [
            {"id": 11, "complete_name": "MTY/Stock", "usage": "internal", "parent_path": "10/11/"},
            {"id": 12, "complete_name": "MTY/Taller Mantenimiento", "usage": "internal", "parent_path": "10/12/"},
            {"id": 13, "complete_name": "MTY/Rent In", "usage": "internal", "parent_path": "10/13/"},
            {"id": 21, "complete_name": "SAL/Stock", "usage": "internal", "parent_path": "20/21/"},
            {"id": 50, "complete_name": "Renta MTY", "usage": "customer", "parent_path": "5/50/"},
            {"id": 60, "complete_name": "Renta SAL", "usage": "customer", "parent_path": "5/60/"},
        ],
        "stock.quant": [
            {"id": 1, "product_id": [30, "Tijera 19ft"], "location_id": [11, "MTY/Stock"], "quantity": 3,
             "lot_id": [900, "T19-001"], "company_id": [1, "PROSIC"]},
            {"id": 2, "product_id": [30, "Tijera 19ft"], "location_id": [50, "Renta MTY"], "quantity": 5,
             "lot_id": [901, "T19-002"], "company_id": [1, "PROSIC"]},
            {"id": 3, "product_id": [31, "Brazo 45ft"], "location_id": [12, "MTY/Taller"], "quantity": 2,
             "lot_id": [902, "B45-001"], "company_id": [1, "PROSIC"]},
            {"id": 4, "product_id": [31, "Brazo 45ft"], "location_id": [21, "SAL/Stock"], "quantity": 10,
             "lot_id": [903, "B45-002"], "company_id": [1, "PROSIC"]},
            {"id": 5, "product_id": [30, "Tijera 19ft"], "location_id": [13, "MTY/Rent In"], "quantity": 1,
             "lot_id": [904, "T19-003"], "company_id": [1, "PROSIC"]},
        ],
        "stock.move.line": [
            # T19-001 llegó a MTY/Stock hace 45 días (ociosa)
            {"id": 1, "lot_id": [900, "T19-001"], "location_dest_id": [11, "MTY/Stock"], "date": dt(45), "state": "done"},
            # B45-001: estuvo en MTY/Stock y entró a taller hace 40 días
            {"id": 2, "lot_id": [902, "B45-001"], "location_dest_id": [11, "MTY/Stock"], "date": dt(100), "state": "done"},
            {"id": 3, "lot_id": [902, "B45-001"], "location_dest_id": [12, "MTY/Taller"], "date": dt(40), "state": "done"},
            # B45-002 llegó a Saltillo hace 5 días
            {"id": 4, "lot_id": [903, "B45-002"], "location_dest_id": [21, "SAL/Stock"], "date": dt(5), "state": "done"},
        ],
        "rental.order.contract": [
            {"id": 1, "name": "C001", "rental_start_time": dt(20), "rental_end_time": f"{d(-5)} 18:00:00",
             "closed_time": False, "rental_qty": 1, "product_id": [30, "Tijera 19ft"], "order_id": [1, "S001"],
             "partner_id": [100, "Constructora Alfa SA"], "state": "in_progress", "price_subtotal": 25000,
             "location_dest_id": [50, "Renta MTY"], "company_id": [1, "PROSIC"]},
        ],
        "maintenance.stage": [{"id": 1, "name": "Nueva", "done": False, "cancel": False},
                              {"id": 2, "name": "Reparado", "done": True, "cancel": False}],
        "maintenance.request": [
            {"id": 1, "stage_id": [1, "Nueva"], "maintenance_type": "corrective", "equipment_id": [9, "Tijera #A12"],
             "request_date": d(10), "close_date": False, "branch_office_id": False, "company_id": [1, "PROSIC"]},
            {"id": 2, "stage_id": [2, "Reparado"], "maintenance_type": "preventive", "equipment_id": [9, "Tijera #A12"],
             "request_date": d(12), "close_date": d(8), "branch_office_id": False, "company_id": [1, "PROSIC"]},
        ],
        "repair.order": [
            {"id": 1, "state": "done", "create_date": dt(9), "amount_untaxed": 2500, "lot_id": [9, "Tijera #A12"], "product_id": [30, "Tijera 19ft"],
             "company_id": [1, "PROSIC"]},
        ],
        "purchase.report": [
            {"id": 1, "date_approve": dt(7), "state": "purchase", "untaxed_total": 800, "partner_id": [200, "Refacciones Gama"],
             "order_id": [1, "P001"], "qty_ordered": 4, "qty_received": 2, "qty_to_be_billed": 2, "picking_type_id": [1, "MTY: Recepciones"], "company_id": [1, "PROSIC"]},
            {"id": 2, "date_approve": dt(8), "state": "purchase", "untaxed_total": 200, "partner_id": [201, "Llantas Delta"],
             "order_id": [2, "P002"], "qty_ordered": 1, "qty_received": 1, "qty_to_be_billed": 0, "picking_type_id": [1, "MTY: Recepciones"], "company_id": [1, "PROSIC"]},
        ],
    }


TYPES = {
    "sale.report": {"date": "datetime"},
    "sale.order": {"date_order": "datetime"},
    "rental.order.contract": {"rental_start_time": "datetime", "rental_end_time": "datetime"},
    "purchase.report": {"date_approve": "datetime"},
    "repair.order": {"create_date": "datetime"},
    "account.move": {"invoice_date": "date"},
    "maintenance.request": {"request_date": "date"},
}
RELATIONS = {
    ("sale.report", "order_id"): "sale.order",
    ("stock.quant", "product_id"): "product.product",
    ("maintenance.request", "stage_id"): "maintenance.stage",
    ("rental.order.contract", "order_id"): "sale.order",
    ("account.move", "payment_id"): "account.payment",
}
