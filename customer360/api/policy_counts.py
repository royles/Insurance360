"""Policy row counts scoped to overview / portfolio customer segments."""

from __future__ import annotations

import sqlite3

from customer360.api.segments import segment_scope_sql
from customer360.warehouse import CUSTOMERS, POLICIES, in_force_flag_expr


def policy_totals_for_segment(
    conn: sqlite3.Connection,
    segment: str | None,
) -> tuple[int, int]:
    """Return (total_policy_rows, active_policy_rows) for customers in the segment."""
    where_sql, params = segment_scope_sql(segment)
    row = conn.execute(
        f"""
        WITH scoped AS (
            SELECT c.CUSTOMER_ID
            FROM {CUSTOMERS} c
            WHERE {where_sql}
        )
        SELECT
            COUNT(*) AS total_policies,
            COALESCE(SUM({in_force_flag_expr("p")}), 0) AS active_policies
        FROM {POLICIES} p
        INNER JOIN scoped s ON p.CUSTOMER_ID = s.CUSTOMER_ID
        """,
        params,
    ).fetchone()
    return int(row["total_policies"] or 0), int(row["active_policies"] or 0)
