"""Comisiones (Fase 01): reglas del requerimiento sobre apuntes contables y conciliaciones."""
import asyncio
from datetime import date, timedelta

from fake_odoo import FakeOdoo

from app.cache import Cache
from app.config import get_settings
from app.schemas.kpis import KpiFilters
from app.services.registry import get_module_kpis

TODAY = date(2026, 10, 31)
D = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731
RELATIONS = {("account.partial.reconcile", "debit_move_id"): "account.move.line",
             ("account.move.line", "move_id"): "account.move"}
ACC = {"renta": 32, "maniobra": 2687, "nc_renta": 2701, "cxc": 50, "iva": 60, "anticipo": 20, "accesorios": 2699}


def m(i, n="x"):
    return [i, n]


def build_data() -> dict:
    accounts = [{"id": ACC["renta"], "code": "401-001-001", "internal_type": "other"},
                {"id": ACC["maniobra"], "code": "401-002-001", "internal_type": "other"},
                {"id": ACC["nc_renta"], "code": "402-001-001", "internal_type": "other"},
                {"id": ACC["cxc"], "code": "110-105-001", "internal_type": "receivable"},
                {"id": ACC["iva"], "code": "200-209-001", "internal_type": "other"},
                {"id": ACC["anticipo"], "code": "200-206-001", "internal_type": "other"},
                {"id": ACC["accesorios"], "code": "401-005-009", "internal_type": "other"}]
    journals = [{"id": 1, "type": "sale", "code": "FMTY"}, {"id": 2, "type": "bank", "code": "BX947"},
                {"id": 3, "type": "general", "code": "DR"}, {"id": 4, "type": "sale", "code": "NCMTY"}]
    moves, lines, partials = [], [], []

    def move(mid, name, mtype, inv_date, residual=0.0, journal=1, seller="Ana Ventas", branch="01 Monterrey"):
        moves.append({"id": mid, "name": name, "move_type": mtype, "invoice_date": inv_date, "date": inv_date,
                      "ref": f"SO{mid}", "partner_id": m(900 + mid, f"Cliente {mid}"), "invoice_user_id": m(7, seller),
                      "branch_office_id": m(2 if "Tepotzotl" in branch else 1, branch), "payment_state": "paid", "amount_total_signed": 0,
                      "amount_residual_signed": residual, "journal_id": m(journal), "state": "posted"})

    def line(lid, mid, acc, debit=0.0, credit=0.0, date_=None, journal=1, matched=()):
        mv = next(x for x in moves if x["id"] == mid)
        lines.append({"id": lid, "move_id": m(mid, mv["name"]), "account_id": m(acc, "cta"), "debit": debit,
                      "credit": credit, "date": date_ or mv["date"], "partner_id": mv["partner_id"],
                      "cost_center_id": m(1, "01 Plataformas de Elevación") if acc in (ACC["renta"], ACC["nc_renta"]) else False,
                      "product_id": False, "journal_id": m(journal), "parent_state": "posted",
                      "matched_credit_ids": list(matched)})

    def pay(pid, inv_line, other_line, amount, when):
        partials.append({"id": pid, "debit_move_id": m(inv_line), "credit_move_id": m(other_line), "amount": amount,
                         "max_date": when})

    # A: renta 1000 cobrada en banco → comisiona 1000
    move(1, "FMTY/1", "out_invoice", D(20)); line(11, 1, ACC["renta"], credit=1000); line(12, 1, ACC["cxc"], debit=1160, matched=[101])
    move(2, "BX947/1", "entry", D(5), journal=2); line(21, 2, ACC["cxc"], credit=1160, journal=2)
    pay(101, 12, 21, 1160, D(5))
    # B: renta 500 saldada con aplicación de anticipo (nota de crédito a 200-206) → comisiona; Odoo la marca «Revertido»
    move(3, "FMTY/2", "out_invoice", D(15)); line(31, 3, ACC["renta"], credit=500); line(32, 3, ACC["cxc"], debit=580, matched=[102])
    moves[-1]["payment_state"] = "reversed"
    move(4, "NCMTY/1", "out_refund", D(3), journal=4); line(41, 4, ACC["anticipo"], debit=500, journal=4); line(42, 4, ACC["cxc"], credit=580, journal=4)
    pay(102, 32, 42, 580, D(3))
    # C: renta 300 «pagada» con póliza del diario de operaciones varias → NO comisiona
    move(5, "FMTY/3", "out_invoice", D(25)); line(51, 5, ACC["renta"], credit=300); line(52, 5, ACC["cxc"], debit=348, matched=[103])
    move(6, "DR/1", "entry", D(2), journal=3); line(61, 6, ACC["cxc"], credit=348, journal=3)
    pay(103, 52, 61, 348, D(2))
    # D: renta 200 emitida hace 400 días y cobrada este mes → NO comisiona (+365)
    move(7, "FMTY/4", "out_invoice", D(400)); line(71, 7, ACC["renta"], credit=200); line(72, 7, ACC["cxc"], debit=232, matched=[104])
    move(8, "BX947/2", "entry", D(4), journal=2); line(81, 8, ACC["cxc"], credit=232, journal=2)
    pay(104, 72, 81, 232, D(4))
    # E: maniobra (401-002-001, está en la lista de Finanzas) cobrada → comisiona 100
    move(9, "FMTY/5", "out_invoice", D(10)); line(91, 9, ACC["maniobra"], credit=100); line(92, 9, ACC["cxc"], debit=116, matched=[105])
    move(10, "BX947/3", "entry", D(4), journal=2); line(101, 10, ACC["cxc"], credit=116, journal=2)
    pay(105, 92, 101, 116, D(4))
    # F: renta 400 con pago parcial (saldo pendiente) → aún no comisiona
    move(11, "FMTY/6", "out_invoice", D(10), residual=200); line(111, 11, ACC["renta"], credit=400); line(112, 11, ACC["cxc"], debit=464, matched=[106])
    move(12, "BX947/4", "entry", D(4), journal=2); line(121, 12, ACC["cxc"], credit=264, journal=2)
    pay(106, 112, 121, 264, D(4))
    # G: renta 1000 con nota de crédito de renta por 100 y el resto en banco → comisiona 900 (vendedor/sucursal distintos)
    move(13, "FMTY/7", "out_invoice", D(12), seller="Beto Ventas", branch="02 Tepotzotlán")
    line(131, 13, ACC["renta"], credit=1000); line(132, 13, ACC["cxc"], debit=1160, matched=[107, 108])
    move(14, "NCMTY/2", "out_refund", D(6), journal=4); line(141, 14, ACC["nc_renta"], debit=100, journal=4); line(142, 14, ACC["cxc"], credit=116, journal=4)
    move(15, "BX947/5", "entry", D(1), journal=2); line(151, 15, ACC["cxc"], credit=1044, journal=2)
    pay(107, 132, 142, 116, D(6)); pay(108, 132, 151, 1044, D(1))
    # H: renta pagada en el mes ANTERIOR → no cuenta este mes
    move(16, "FMTY/8", "out_invoice", D(60)); line(161, 16, ACC["renta"], credit=700); line(162, 16, ACC["cxc"], debit=812, matched=[109])
    move(17, "BX947/6", "entry", D(45), journal=2); line(171, 17, ACC["cxc"], credit=812, journal=2)
    pay(109, 162, 171, 812, D(45))
    # I: renta de accesorios (401-005-009, NO está en la lista) cobrada → se ignora
    move(18, "FMTY/9", "out_invoice", D(10)); line(181, 18, ACC["accesorios"], credit=50); line(182, 18, ACC["cxc"], debit=58, matched=[110])
    move(19, "BX947/7", "entry", D(4), journal=2); line(191, 19, ACC["cxc"], credit=58, journal=2)
    pay(110, 182, 191, 58, D(4))
    return {"account.account": accounts, "account.journal": journals, "account.move": moves,
            "account.move.line": lines, "account.partial.reconcile": partials}


def run(**filters):
    odoo = FakeOdoo(build_data(), relations=RELATIONS)
    f = KpiFilters(date_from=date(2026, 10, 1), date_to=TODAY, compare="none", **filters)
    return asyncio.run(get_module_kpis(odoo, Cache(""), get_settings(), "comisiones", f, use_cache=False))


def tables(k):
    return {t.key: t for t in k.tables}


def test_commission_base_follows_document_rules():
    k = run()
    assert k.available, k.reason
    inc = {r["factura"]: r for r in tables(k)["facturas_comisionan"].rows}
    assert set(inc) == {"FMTY/1", "FMTY/2", "FMTY/5", "FMTY/7"}
    assert inc["FMTY/1"]["base"] == 1000 and inc["FMTY/1"]["cobro"] == "Dinero"
    assert inc["FMTY/2"]["base"] == 500 and inc["FMTY/2"]["cobro"] == "Aplicación de anticipo"
    assert inc["FMTY/7"]["base"] == 900 and inc["FMTY/7"]["nc"] == -100
    assert inc["FMTY/7"]["fecha_cobro"] == D(1)  # la fecha en que quedó saldada (último pago)
    cards = {c.key: c for c in k.cards}
    assert inc["FMTY/5"]["base"] == 100
    assert cards["base_comision"].value == 2500 and cards["facturas_cobradas"].value == 4


def test_excluded_invoices_have_reason():
    exc = {r["factura"]: r["motivo"] for r in tables(run())["facturas_excluidas"].rows}
    assert exc == {"FMTY/3": "Saldada con póliza de diario (cancelación / ajuste, no es cobro)",
                   "FMTY/4": "Cobrada más de 365 días después de la emisión"}


def test_base_by_seller_and_branch():
    rows = {(r["vendedor"], r["sucursal"]): r for r in tables(run())["base_vendedor_sucursal"].rows}
    assert rows[("Ana Ventas", "01 Monterrey")]["base"] == 1600 and rows[("Ana Ventas", "01 Monterrey")]["facturas"] == 3
    assert rows[("Beto Ventas", "02 Tepotzotlán")]["base"] == 900


def test_billing_detail_is_rental_lines_by_accounting_date():
    t = tables(run())["detalle_facturacion"]
    asientos = {r["asiento"] for r in t.rows}
    assert "FMTY/5" in asientos  # maniobra: está en la lista
    assert "FMTY/9" not in asientos  # renta de accesorios: no está en la lista
    assert "FMTY/4" not in asientos  # emitida hace 400 días: fuera del periodo contable
    nc = next(r for r in t.rows if r["asiento"] == "NCMTY/2")
    assert nc["neto"] == -100 and nc["debito"] == 100


def test_branch_filter_uses_invoice_branch():
    k = run(branch_id=1)
    assert {r["factura"] for r in tables(k)["facturas_comisionan"].rows} == {"FMTY/1", "FMTY/2", "FMTY/5"}


def test_commissions_detail_not_sent_to_ai():
    from app.ai.anonymizer import Anonymizer, ai_payload

    payload = ai_payload(run(), Anonymizer(True))
    text = str(payload)
    assert "Cliente 1" not in text and "FMTY/1" not in text  # sin detalle por factura ni clientes
    assert "Ana Ventas" not in text  # vendedores anonimizados


def test_base_by_account_matches_finance_list():
    rows = {r["cuenta"].split()[0]: r for r in tables(run())["base_cuenta"].rows}
    assert rows["401-001-001"]["base"] == 2500  # renta de A, B y G
    assert rows["401-002-001"]["base"] == 100  # maniobra de E
    assert rows["402-001-001"]["base"] == -100  # nota de crédito de G
    assert rows["Total"]["base"] == 2500
    assert "401-005-009" not in rows
