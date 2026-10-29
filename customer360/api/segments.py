"""Customer list segments tied to overview cards."""

from __future__ import annotations

import sqlite3
from typing import Literal

from customer360.warehouse import (
    CUSTOMERS,
    FORECLOSURES,
    INVESTMENTS,
    POLICIES,
    POLICY_STATUS,
    active_policy_predicate,
)

SegmentKey = Literal[
    "customers_all",
    "with_policies",
    "with_foreclosures",
    "with_investments",
    "with_insurance_status",
    "with_market_products",
]

OVERVIEW_DOMAINS: list[tuple[str, str, str, str]] = [
    (
        "Customers (current)",
        "customers_all",
        "All active customer dimension rows",
        f"SELECT COUNT(*) FROM {CUSTOMERS}",
    ),
    (
        "Policies",
        "with_policies",
        "Filter to customers with policies. Card shows total policy rows, active policies, and average per customer.",
        f"""
        SELECT COUNT(DISTINCT c.CUSTOMER_ID)
        FROM {CUSTOMERS} c
        INNER JOIN {POLICIES} p ON p.CUSTOMER_ID = c.CUSTOMER_ID
        """,
    ),
    (
        "Foreclosures",
        "with_foreclosures",
        "Customers with legal encumbrance records",
        f"""
        SELECT COUNT(DISTINCT c.CUSTOMER_ID)
        FROM {CUSTOMERS} c
        INNER JOIN {FORECLOSURES} f ON f.CUSTOMER_ID = c.CUSTOMER_ID
        """,
    ),
    (
        "Policy investment snapshots",
        "with_investments",
        "Customers with policy investment balances",
        f"""
        SELECT COUNT(DISTINCT c.CUSTOMER_ID)
        FROM {CUSTOMERS} c
        INNER JOIN {INVESTMENTS} pit ON pit.CUSTOMER_ID = c.CUSTOMER_ID
        """,
    ),
    (
        "Market tracks",
        "with_market_products",
        "Customers with pension, provident, or life investment products",
        f"""
        SELECT COUNT(DISTINCT c.CUSTOMER_ID)
        FROM {CUSTOMERS} c
        INNER JOIN {POLICIES} p ON p.CUSTOMER_ID = c.CUSTOMER_ID
        WHERE p.PRODUCT_CODE != 301
          AND {active_policy_predicate("p")}
        """,
    ),
    (
        "Insurance status rows",
        "with_insurance_status",
        "Customers with policy status / coverage snapshots",
        f"""
        SELECT COUNT(DISTINCT c.CUSTOMER_ID)
        FROM {CUSTOMERS} c
        INNER JOIN {POLICY_STATUS} ps ON ps.CUSTOMER_ID = c.CUSTOMER_ID
        """,
    ),
]

_ACTIVE = active_policy_predicate("p2")

SEGMENT_WHERE: dict[str, str] = {
    "customers_all": "1=1",
    "with_policies": f"""
        EXISTS (
            SELECT 1 FROM {POLICIES} p2
            WHERE p2.CUSTOMER_ID = c.CUSTOMER_ID
        )
    """,
    "with_foreclosures": f"""
        EXISTS (
            SELECT 1 FROM {FORECLOSURES} f
            WHERE f.CUSTOMER_ID = c.CUSTOMER_ID
        )
    """,
    "with_investments": f"""
        EXISTS (
            SELECT 1 FROM {INVESTMENTS} pit
            WHERE pit.CUSTOMER_ID = c.CUSTOMER_ID
        )
    """,
    "with_market_products": f"""
        EXISTS (
            SELECT 1 FROM {POLICIES} p2
            WHERE p2.CUSTOMER_ID = c.CUSTOMER_ID
              AND p2.PRODUCT_CODE != 301
              AND {_ACTIVE}
        )
    """,
    "with_insurance_status": f"""
        EXISTS (
            SELECT 1 FROM {POLICY_STATUS} ps
            WHERE ps.CUSTOMER_ID = c.CUSTOMER_ID
        )
    """,
}


def normalize_segment(segment: str | None) -> str:
    key = (segment or "customers_all").strip()
    if key not in SEGMENT_WHERE:
        return "customers_all"
    return key


def segment_scope_sql(
    segment: str | None,
    *,
    alias: str = "c",
    customer_id: int | None = None,
) -> tuple[str, list[object]]:
    """WHERE fragment for current customers in a cohort (or one customer by id)."""
    if customer_id is not None:
        return f"{alias}.CUSTOMER_ID = ?", [customer_id]
    seg = normalize_segment(segment)
    return f"({SEGMENT_WHERE[seg]})", []


def current_customer_exists(conn: sqlite3.Connection, customer_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM DIM_CUSTOMERS
        WHERE CUSTOMER_ID = ?
        """,
        (customer_id,),
    ).fetchone()
    return row is not None
