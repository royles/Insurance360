"""Physical warehouse names and SQL fragments for the slim insurance schema."""

from __future__ import annotations

CUSTOMERS = "DIM_CUSTOMERS"
POLICIES = "DIM_POLICIES"
POLICY_STATUS = "FCT_POLICY_STATUS"
FORECLOSURES = "FCT_POLICY_FORECLOSURES"
INVESTMENTS = "FCT_POLICY_INVESTMENTS"
FUNDS = "FCT_INVESTMENT_FUNDS"

ACTIVE_POLICY_STATUSES = ("Active", "Premium Paying", "Paid Up")

# Product codes that are savings / protection contracts other than elementary property.
MARKET_PRODUCT_CODES = (101, 102, 201, 401, 402, 403)


def customer_name_expr(alias: str = "c") -> str:
    return (
        f"TRIM(COALESCE({alias}.FIRST_NAME, '') || ' ' || COALESCE({alias}.SURNAME, ''))"
    )


def active_policy_predicate(alias: str = "p") -> str:
    quoted = ", ".join(f"'{status}'" for status in ACTIVE_POLICY_STATUSES)
    return f"{alias}.POLICY_STATUS IN ({quoted})"


def policy_status_code_expr(alias: str = "p") -> str:
    return (
        f"CASE {alias}.POLICY_STATUS "
        "WHEN 'Active' THEN 10 "
        "WHEN 'Premium Paying' THEN 20 "
        "WHEN 'Paid Up' THEN 45 "
        "WHEN 'Lapsed' THEN 55 "
        "WHEN 'Surrendered' THEN 60 "
        "ELSE 0 END"
    )


def in_force_flag_expr(alias: str = "p") -> str:
    return f"CASE WHEN {active_policy_predicate(alias)} THEN 1 ELSE 0 END"
