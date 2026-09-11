"""ERPNext payment-ledger CTE compatibility override for PostgreSQL.

Backports the PostgreSQL-safe ``QueryPaymentLedger.query_for_outstanding``
behavior from ERPNext develop commit 8154c45bf0. Older releases select
non-grouped Payment Ledger Entry columns in aggregate CTEs, which MariaDB
accepts but PostgreSQL rejects.
"""

import inspect
from importlib import import_module

NAME = "erpnext_payment_ledger_grouping"
UPSTREAM_COMMIT = "8154c45bf0"

_original_query_for_outstanding = None
_patched_query_for_outstanding = None


def _load_accounts_utils():
    try:
        return import_module("erpnext.accounts.utils")
    except ImportError:
        return None


def _upstream_is_compatible(method):
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return False
    return (
        ".groupby(ple.account, ple.voucher_type" in source
        and 'Min(ple.name).as_("representative")' in source
        and 'as_("representative_ple")' in source
    )


def is_needed():
    module = _load_accounts_utils()
    query_payment_ledger = getattr(module, "QueryPaymentLedger", None) if module else None
    return (
        query_payment_ledger is not None
        and not is_applied()
        and not _upstream_is_compatible(query_payment_ledger.query_for_outstanding)
    )


def is_applied():
    module = _load_accounts_utils()
    query_payment_ledger = getattr(module, "QueryPaymentLedger", None) if module else None
    return (
        query_payment_ledger is not None
        and _patched_query_for_outstanding is not None
        and query_payment_ledger.query_for_outstanding is _patched_query_for_outstanding
    )


def _compatible_query_for_outstanding(self):
    module = _load_accounts_utils()
    qb = module.qb
    Criterion, Table, AliasedQuery = module.Criterion, module.Table, module.AliasedQuery
    from frappe.query_builder.functions import Min

    Case, Max, Sum = module.Case, module.Max, module.Sum
    ple = self.ple
    filter_on_voucher_no = []
    filter_on_against_voucher_no = []

    if self.vouchers:
        voucher_types = {x.voucher_type for x in self.vouchers}
        voucher_nos = {x.voucher_no for x in self.vouchers}
        filter_on_voucher_no += [ple.voucher_type.isin(voucher_types), ple.voucher_no.isin(voucher_nos)]
        filter_on_against_voucher_no += [
            ple.against_voucher_type.isin(voucher_types),
            ple.against_voucher_no.isin(voucher_nos),
        ]
    if self.voucher_no:
        filter_on_voucher_no.append(ple.voucher_no.like(f"%{self.voucher_no}%"))
        filter_on_against_voucher_no.append(ple.against_voucher_no.like(f"%{self.voucher_no}%"))

    filter_on_outstanding_amount = []
    if self.min_outstanding:
        field = Table("outstanding").amount_in_account_currency
        filter_on_outstanding_amount.append(
            field >= self.min_outstanding if self.min_outstanding > 0 else field <= self.min_outstanding
        )
    if self.max_outstanding:
        field = Table("outstanding").amount_in_account_currency
        filter_on_outstanding_amount.append(
            field <= self.max_outstanding if self.max_outstanding > 0 else field >= self.max_outstanding
        )

    if self.limit and self.get_invoices:
        outstanding_vouchers = (
            qb.from_(ple)
            .select(
                ple.against_voucher_no.as_("voucher_no"),
                Sum(ple.amount_in_account_currency).as_("amount_in_account_currency"),
                Max(
                    Case().when(
                        (ple.voucher_no == ple.against_voucher_no)
                        & (ple.voucher_type == ple.against_voucher_type),
                        ple.posting_date,
                    )
                ).as_("invoice_date"),
            )
            .where(ple.delinked == 0)
            .where(Criterion.all(filter_on_against_voucher_no))
            .where(Criterion.all(self.common_filter))
            .where(Criterion.all(self.dimensions_filter))
            .where(Criterion.all(self.voucher_posting_date))
            .groupby(ple.against_voucher_type, ple.against_voucher_no, ple.party_type, ple.party)
            .orderby(qb.Field("invoice_date"), qb.Field("voucher_no"))
            .having(Sum(ple.amount_in_account_currency) > 0)
            .limit(self.limit)
            .run()
        )
        if outstanding_vouchers:
            voucher_nos = [x[0] for x in outstanding_vouchers]
            filter_on_voucher_no.append(ple.voucher_no.isin(voucher_nos))
            filter_on_against_voucher_no.append(ple.against_voucher_no.isin(voucher_nos))

    grouped = (
        qb.from_(ple)
        .select(
            ple.account,
            ple.voucher_type,
            ple.voucher_no,
            ple.party_type,
            ple.party,
            Max(ple.posting_date).as_("posting_date"),
            Max(ple.due_date).as_("due_date"),
            Max(ple.account_currency).as_("currency"),
            Sum(ple.amount).as_("amount"),
            Sum(ple.amount_in_account_currency).as_("amount_in_account_currency"),
            Min(ple.name).as_("representative"),
        )
        .where(ple.delinked == 0)
        .where(Criterion.all(filter_on_voucher_no))
        .where(Criterion.all(self.common_filter))
        .where(Criterion.all(self.dimensions_filter))
        .where(Criterion.all(self.voucher_posting_date))
        .groupby(ple.account, ple.voucher_type, ple.voucher_no, ple.party_type, ple.party)
    ).as_("grouped")
    representative_ple = qb.DocType("Payment Ledger Entry").as_("representative_ple")
    voucher_amount = (
        qb.from_(grouped)
        .inner_join(representative_ple)
        .on(representative_ple.name == grouped.representative)
        .select(
            grouped.account,
            grouped.voucher_type,
            grouped.voucher_no,
            grouped.party_type,
            grouped.party,
            grouped.posting_date,
            grouped.due_date,
            grouped.currency,
            grouped.amount,
            grouped.amount_in_account_currency,
            representative_ple.cost_center.as_("cost_center"),
            representative_ple.remarks.as_("remarks"),
        )
    )
    outstanding = (
        qb.from_(ple)
        .select(
            ple.account,
            ple.against_voucher_type.as_("voucher_type"),
            ple.against_voucher_no.as_("voucher_no"),
            ple.party_type,
            ple.party,
            Max(ple.posting_date).as_("posting_date"),
            Max(ple.due_date).as_("due_date"),
            Max(ple.account_currency).as_("currency"),
            Sum(ple.amount).as_("amount"),
            Sum(ple.amount_in_account_currency).as_("amount_in_account_currency"),
        )
        .where(ple.delinked == 0)
        .where(Criterion.all(filter_on_against_voucher_no))
        .where(Criterion.all(self.common_filter))
        .groupby(ple.account, ple.against_voucher_type, ple.against_voucher_no, ple.party_type, ple.party)
    )

    vouchers = AliasedQuery("vouchers")
    outs = AliasedQuery("outstanding")
    query = (
        qb.with_(voucher_amount, "vouchers")
        .with_(outstanding, "outstanding")
        .from_(vouchers)
        .left_join(outs)
        .on(
            (vouchers.account == outs.account)
            & (vouchers.voucher_type == outs.voucher_type)
            & (vouchers.voucher_no == outs.voucher_no)
            & (vouchers.party_type == outs.party_type)
            & (vouchers.party == outs.party)
        )
        .select(
            Table("vouchers").account,
            Table("vouchers").voucher_type,
            Table("vouchers").voucher_no,
            Table("vouchers").party_type,
            Table("vouchers").party,
            Table("vouchers").posting_date,
            Table("vouchers").amount.as_("invoice_amount"),
            Table("vouchers").amount_in_account_currency.as_("invoice_amount_in_account_currency"),
            Table("outstanding").amount.as_("outstanding"),
            Table("outstanding").amount_in_account_currency.as_("outstanding_in_account_currency"),
            (Table("vouchers").amount - Table("outstanding").amount).as_("paid_amount"),
            (
                Table("vouchers").amount_in_account_currency - Table("outstanding").amount_in_account_currency
            ).as_("paid_amount_in_account_currency"),
            Table("vouchers").due_date,
            Table("vouchers").currency,
            Table("vouchers").cost_center.as_("cost_center"),
            Table("vouchers").remarks,
        )
        .where(Criterion.all(filter_on_outstanding_amount))
    )
    if self.get_invoices:
        query = query.where(Table("outstanding").amount_in_account_currency > 0)
    elif self.get_payments:
        query = query.where(Table("outstanding").amount_in_account_currency < 0)
    if self.limit:
        query = query.limit(self.limit)
    self.cte_query_voucher_amount_and_outstanding = query
    self.voucher_outstandings = query.run(as_dict=True)


def apply():
    global _original_query_for_outstanding, _patched_query_for_outstanding
    module = _load_accounts_utils()
    query_payment_ledger = getattr(module, "QueryPaymentLedger", None) if module else None
    if (
        query_payment_ledger is None
        or is_applied()
        or _upstream_is_compatible(query_payment_ledger.query_for_outstanding)
    ):
        return False
    _original_query_for_outstanding = query_payment_ledger.query_for_outstanding
    _patched_query_for_outstanding = _compatible_query_for_outstanding
    query_payment_ledger.query_for_outstanding = _compatible_query_for_outstanding  # nosemgrep
    return True


def remove():
    global _original_query_for_outstanding, _patched_query_for_outstanding
    module = _load_accounts_utils()
    query_payment_ledger = getattr(module, "QueryPaymentLedger", None) if module else None
    if query_payment_ledger is None or not is_applied():
        return False
    query_payment_ledger.query_for_outstanding = _original_query_for_outstanding  # nosemgrep
    _original_query_for_outstanding = None
    _patched_query_for_outstanding = None
    return True
