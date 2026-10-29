"""Product catalog grouped by class for heatmap-style navigation."""

from __future__ import annotations

import sqlite3

from customer360.api.customer_list import normalize_city_name
from customer360.api.segments import normalize_segment, segment_scope_sql
from customer360.warehouse import CUSTOMERS, POLICIES, in_force_flag_expr

PRODUCT_CLASSES: list[tuple[str, str, tuple[int, ...]]] = [
    ("life_protection", "Life & protection", (101, 102)),
    ("health", "Health & supplementary", (201,)),
    ("property", "Property & elementary", (301,)),
    ("long_term_savings", "Long-term savings", (401, 402, 403)),
]


def fetch_product_catalog_city_options(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT c.CITY AS city_name
        FROM {CUSTOMERS} c
        WHERE c.CITY IS NOT NULL
          AND TRIM(c.CITY) != ''
        ORDER BY city_name ASC
        """
    ).fetchall()
    return [str(r["city_name"]) for r in rows]


def fetch_product_catalog(
    conn: sqlite3.Connection,
    *,
    segment: str | None = None,
    city: str | None = None,
) -> dict:
    seg = normalize_segment(segment)
    city_canon = normalize_city_name(city)
    customer_where, params = segment_scope_sql(seg)
    if city_canon:
        customer_where += " AND c.CITY = ?"
        params.append(city_canon)

    rows = conn.execute(
        f"""
        SELECT
            p.PRODUCT_CODE AS policy_type_code,
            MAX(p.PRODUCT_NAME) AS policy_type_desc,
            COUNT(DISTINCT p.CUSTOMER_ID) AS customer_count,
            COUNT(*) AS policy_count,
            SUM({in_force_flag_expr("p")}) AS active_policy_count
        FROM {POLICIES} p
        INNER JOIN {CUSTOMERS} c
            ON c.CUSTOMER_ID = p.CUSTOMER_ID
        WHERE {customer_where}
        GROUP BY p.PRODUCT_CODE
        ORDER BY customer_count DESC, policy_type_code ASC
        """,
        params,
    ).fetchall()

    by_code = {
        int(r["policy_type_code"]): {
            "policy_type_code": int(r["policy_type_code"]),
            "policy_type_desc": str(r["policy_type_desc"] or ""),
            "customer_count": int(r["customer_count"] or 0),
            "policy_count": int(r["policy_count"] or 0),
            "active_policy_count": int(r["active_policy_count"] or 0),
        }
        for r in rows
    }

    max_customers = max((p["customer_count"] for p in by_code.values()), default=0)

    classes: list[dict] = []
    for class_key, class_label, codes in PRODUCT_CLASSES:
        products = [by_code[c] for c in codes if c in by_code]
        customer_total = sum(p["customer_count"] for p in products)
        classes.append(
            {
                "class_key": class_key,
                "class_label": class_label,
                "customer_count": customer_total,
                "policy_count": sum(p["policy_count"] for p in products),
                "products": products,
            }
        )

    return {
        "max_customer_count": max_customers,
        "classes": classes,
        "filters": {
            "segment": seg,
            "city": city_canon,
        },
        "city_options": fetch_product_catalog_city_options(conn),
    }
