"""ERPNext PostgreSQL-safe Sales Order payment-terms invoice allocation."""

import inspect
from importlib import import_module

NAME = "erpnext_payment_terms_status"
_original = None
_patched = None


def _load():
    try:
        return import_module(
            "erpnext.selling.report.payment_terms_status_for_sales_order.payment_terms_status_for_sales_order"
        )
    except ImportError:
        return None


def is_applied():
    module = _load()
    return module is not None and _patched is not None and module.get_so_with_invoices is _patched


def is_needed():
    module = _load()
    if module is None or is_applied():
        return False
    try:
        source = inspect.getsource(module.get_so_with_invoices)
    except (OSError, TypeError):
        return False
    return ".groupby(sii.parent)" in source and ".groupby(sii.parent, sii.sales_order)" not in source


def apply():
    global _original, _patched
    if not is_needed():
        return False
    module = _load()
    from frappe import qb, query_builder
    from frappe.query_builder import Criterion
    from frappe.query_builder.functions import Max, Sum
    from frappe.utils import flt

    _original = module.get_so_with_invoices

    def allocate(invoices):
        rows_by_invoice = {}
        for row in invoices:
            rows_by_invoice.setdefault(row.invoice, []).append(row)
        for rows in rows_by_invoice.values():
            rows.sort(key=lambda row: row.sales_order)
            total_net = sum(flt(row.order_net_amount) for row in rows)
            grand_total = flt(rows[0].invoice_grand_total)
            if not total_net:
                for row in rows:
                    row.invoice_amount = grand_total / len(rows)
                continue
            allocated = 0.0
            for row in rows[:-1]:
                row.invoice_amount = grand_total * flt(row.order_net_amount) / total_net
                allocated += row.invoice_amount
            rows[-1].invoice_amount = grand_total - allocated

    def compatible(filters):
        so = qb.DocType("Sales Order")
        ps = qb.DocType("Payment Schedule")
        soi = qb.DocType("Sales Order Item")
        conditions = module.get_conditions(filters)
        filter_criterions = module.build_filter_criterions(filters)
        datediff = query_builder.CustomFunction("DATEDIFF", ["cur_date", "due_date"])
        ifelse = query_builder.CustomFunction("IF", ["condition", "then", "else"])
        query_so = (
            qb.from_(so)
            .join(soi)
            .on(soi.parent == so.name)
            .join(ps)
            .on(ps.parent == so.name)
            .select(so.name)
            .distinct()
            .select(
                so.customer,
                so.transaction_date.as_("submitted"),
                ifelse(datediff(ps.due_date, module.functions.CurDate()) < 0, "Overdue", "Unpaid").as_(
                    "status"
                ),
                ps.payment_term,
                ps.description,
                ps.due_date,
                ps.invoice_portion,
                ps.base_payment_amount,
                ps.paid_amount,
            )
            .where(
                (so.docstatus == 1)
                & (so.status.isin(["To Deliver and Bill", "To Bill", "To Pay"]))
                & (so.company == conditions.company)
                & (so.transaction_date[conditions.start_date : conditions.end_date])
            )
            .where(Criterion.all(filter_criterions))
            .orderby(so.name, so.transaction_date, ps.due_date)
        )
        sorders = query_so.run(as_dict=True)
        invoices = []
        if sorders:
            soi = qb.DocType("Sales Order Item")
            si = qb.DocType("Sales Invoice")
            sii = qb.DocType("Sales Invoice Item")
            query_inv = (
                qb.from_(sii)
                .right_join(si)
                .on(si.name == sii.parent)
                .inner_join(soi)
                .on(soi.name == sii.so_detail)
                .select(
                    sii.sales_order.as_("sales_order"),
                    sii.parent.as_("invoice"),
                    Sum(sii.base_net_amount).as_("order_net_amount"),
                    Max(si.base_grand_total).as_("invoice_grand_total"),
                )
                .where((sii.sales_order.isin([row.name for row in sorders])) & (si.docstatus == 1))
                .groupby(sii.parent, sii.sales_order)
            )
            invoices = query_inv.run(as_dict=True)
            allocate(invoices)
        return sorders, invoices

    _patched = compatible
    module.get_so_with_invoices = compatible  # nosemgrep
    return True


def remove():
    global _original, _patched
    module = _load()
    if module is None or not is_applied():
        return False
    module.get_so_with_invoices = _original  # nosemgrep
    _original = None
    _patched = None
    return True
