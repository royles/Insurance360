#!/usr/bin/env python3
"""Create and seed the Customer 360 SQLite warehouse."""

from __future__ import annotations

import argparse
import calendar
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from customer360.interactions.seed import build_interaction_events
from customer360.admin_store import ensure_admin_schema
from customer360.kpi_benchmarks import ensure_kpi_benchmark_catalog
from customer360.admin_preserve import backup_admin_state, restore_admin_state
from customer360.data_source import ensure_sqlite_warehouse_path_registered, resolve_sqlite_warehouse_path
from customer360.paths import default_db_path, project_root, schema_path
from customer360.seed_context import (
    require_seed_material,
    require_territory,
    set_active_territory,
    set_seed_language,
    set_seed_material,
)
from customer360.seed_material import resolve_seed_material
from customer360.territory import get_territory, resolve_territory_id
from customer360.territory.languages import resolve_seed_language
from customer360.territory_store import write_territory_config
from customer360.warehouse import ACTIVE_POLICY_STATUSES

INTERACTIONS_SCHEMA = project_root() / "data" / "interactions_schema.sql"

RNG = random.Random(36085)

POLICY_TYPES = [
    (101, "Life Insurance"),
    (102, "Critical Illness"),
    (201, "Health Supplementary"),
    (301, "Elementary Property"),
    (401, "Pension Fund"),
    (402, "Provident Fund"),
    (403, "Study Fund"),
]

POLICY_STATUS_ACTIVE = ("Active", "Premium Paying", "Paid Up")
POLICY_STATUS_INACTIVE = ("Lapsed", "Surrendered")
POLICY_STATUS = POLICY_STATUS_ACTIVE + POLICY_STATUS_INACTIVE

OCCUPATIONS = (
    "Professional",
    "Clerical",
    "Manual",
    "Self-employed",
    "Retired",
    "Student",
)

CUSTOMER_COLUMNS = (
    "CUSTOMER_ID",
    "CUSTOMER_TYPE",
    "FIRST_NAME",
    "SURNAME",
    "MARITAL_STATUS",
    "OCCUPATION",
    "EMAIL",
    "PHONE",
    "LAST_LOGIN",
    "CITY",
)

POLICY_COLUMNS = (
    "POLICY_NUMBER",
    "CUSTOMER_ID",
    "PRODUCT_CODE",
    "PRODUCT_NAME",
    "POLICY_STATUS",
    "INCEPTION_DATE",
    "EXPIRY_DATE",
    "GROSS_MONTHLY_PREMIUM",
    "CONTRACT_TYPE",
    "EMPLOYER_NAME",
)

# Portfolio mix: ~72% engaged, ~20% stable/watch, ~8% at-risk (industry-like spread).
SEGMENT_WEIGHTS = ("engaged", 0.72), ("stable", 0.20), ("at_risk", 0.08)

# Book shape: skew customers toward investments, coverage, or mixed (demo chart variation).
PRODUCT_MIX_WEIGHTS = (
    ("balanced", 0.40),
    ("investment_focus", 0.22),
    ("insurance_focus", 0.22),
    ("pension_only", 0.08),
    ("property_only", 0.08),
)

INVESTMENT_POLICY_TYPES = (401, 402, 403)
LIFE_HEALTH_POLICY_TYPES = (101, 102, 201)
PROPERTY_POLICY_TYPE = 301


def _assign_engagement_segment() -> str:
    roll = RNG.random()
    cumulative = 0.0
    for name, weight in SEGMENT_WEIGHTS:
        cumulative += weight
        if roll <= cumulative:
            return name
    return "engaged"


def _assign_product_mix() -> str:
    roll = RNG.random()
    cumulative = 0.0
    for name, weight in PRODUCT_MIX_WEIGHTS:
        cumulative += weight
        if roll <= cumulative:
            return name
    return "balanced"


def build_decorrelated_macro_series(
    months: list[date],
    rng: random.Random | None = None,
) -> tuple[list[float], list[float]]:
    """
    Monthly return shocks for investment AUM vs insurance/coverage snapshots.
    Weakly correlated so book charts and objective trends diverge over time.
    Each value is a fractional month-over-month change (e.g. 0.012 = +1.2%).
    """
    r = rng or RNG
    inv_drift = r.uniform(-0.0015, 0.0035)
    ins_drift = r.uniform(-0.0012, 0.0030)
    inv_returns: list[float] = []
    ins_returns: list[float] = []
    for _ in months:
        inv_shock = r.gauss(0, 0.011)
        ins_shock = r.gauss(0, 0.011)
        inv_returns.append(inv_drift + inv_shock)
        # ~30% of months insurance moves opposite the investment book shock (demo chart contrast).
        if r.random() < 0.30:
            ins_returns.append(ins_drift - inv_shock * 0.55 + ins_shock)
        else:
            ins_returns.append(ins_drift + ins_shock * 0.35 + ins_shock)
    return inv_returns, ins_returns


def _policy_types_for_mix(product_mix: str, n_policies: int) -> list[int]:
    """Pick policy type codes so cohorts (investments vs insurance status) overlap less."""
    if product_mix == "property_only":
        return [PROPERTY_POLICY_TYPE] * max(1, min(n_policies, 2))
    if product_mix == "pension_only":
        count = max(1, min(n_policies, 3))
        return [RNG.choice(INVESTMENT_POLICY_TYPES) for _ in range(count)]
    if product_mix == "investment_focus":
        types: list[int] = [
            RNG.choice(INVESTMENT_POLICY_TYPES)
            for _ in range(max(1, n_policies - 1))
        ]
        if n_policies > len(types):
            types.append(RNG.choice([101, 401, 402]))
        return types[:n_policies]
    if product_mix == "insurance_focus":
        pool = list(LIFE_HEALTH_POLICY_TYPES) + [PROPERTY_POLICY_TYPE]
        types = [RNG.choice(pool) for _ in range(max(1, n_policies))]
        if n_policies >= 2 and PROPERTY_POLICY_TYPE not in types and RNG.random() < 0.45:
            types[-1] = PROPERTY_POLICY_TYPE
        return types[:n_policies]
    # balanced — varied book
    types = []
    for _ in range(n_policies):
        roll = RNG.random()
        if roll < 0.35:
            types.append(RNG.choice(INVESTMENT_POLICY_TYPES))
        elif roll < 0.75:
            types.append(RNG.choice(LIFE_HEALTH_POLICY_TYPES))
        else:
            types.append(PROPERTY_POLICY_TYPE)
    return types


def _rows_for_columns(rows: list[dict], columns: tuple[str, ...]) -> list[dict]:
    return [{col: row.get(col) for col in columns} for row in rows]


def _customer_rows_for_db(customers: list[dict]) -> list[dict]:
    return _rows_for_columns(customers, CUSTOMER_COLUMNS)


def _policy_rows_for_db(policies: list[dict]) -> list[dict]:
    return _rows_for_columns(policies, POLICY_COLUMNS)


def _fund_company(product_code: int) -> int:
    return {101: 1, 102: 1, 201: 1, 301: 9, 401: 7, 402: 8, 403: 8}[product_code]


def _in_force(status: str) -> bool:
    return status in ACTIVE_POLICY_STATUSES

def iso(d: date | datetime) -> str:
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d %H:%M:%S")
    return d.isoformat()


def last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


# Book analytics window: ~5 years of monthly snapshots (Jan 2021 → Sep 2025).
BOOK_HISTORY_START = date(2021, 1, 1)
BOOK_AS_OF_DATE = date(2025, 9, 15)


def iter_history_months(
    start: date = BOOK_HISTORY_START,
    end: date | None = None,
) -> list[date]:
    """First day of each month from start through end (inclusive)."""
    end_month = end or date(BOOK_AS_OF_DATE.year, BOOK_AS_OF_DATE.month, 1)
    months: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end_month.year, end_month.month):
        months.append(date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return months


def parse_policy_start(pol: dict) -> date:
    raw = str(pol.get("INCEPTION_DATE") or "")[:10]
    y, m, d = (int(raw[0:4]), int(raw[5:7]), int(raw[8:10])) if len(raw) >= 10 else (2021, 1, 1)
    return date(y, m, min(d, last_day_of_month(y, m).day))


def snapshot_on_or_after_policy_start(policy_start: date, snap: date) -> bool:
    return (snap.year, snap.month) >= (policy_start.year, policy_start.month)


DEFAULT_SEED_CUSTOMERS = 2000
MAX_SEED_CUSTOMERS = 5000


def build_customers(n: int = DEFAULT_SEED_CUSTOMERS) -> list[dict]:
    territory = require_territory()
    material = require_seed_material()
    rows: list[dict] = []
    used_ids: set[int] = set()
    now = datetime(2025, 9, 15, 10, 30, 0)
    first_names = material.first_names
    last_names = material.last_names
    cities = material.cities

    for i in range(n):
        cid = territory.generate_customer_id(RNG)
        while cid in used_ids:
            cid = territory.generate_customer_id(RNG)
        used_ids.add(cid)

        corp_slots = max(2, n // 200)
        customer_type = (
            "Individual" if i < n - corp_slots else RNG.choice(["Individual", "Organization"])
        )
        first = first_names[i % len(first_names)]
        last = last_names[(i // len(first_names)) % len(last_names)]
        city, _street, _zipcode = cities[i % len(cities)]
        marital_code = RNG.choice(list(material.marital_map.keys()))
        prefix = RNG.choice(territory.mobile_prefixes)
        mobile = f"{prefix}-{RNG.randint(1000000, 9999999)}"
        email = f"{first.lower()}.{last.lower()}{i}@{territory.email_domain}"
        segment = _assign_engagement_segment()
        if segment == "engaged":
            registered = 1 if RNG.random() < 0.88 else 0
            last_login = (
                now - timedelta(days=RNG.randint(1, 45)) if registered else None
            )
        elif segment == "stable":
            registered = 1 if RNG.random() < 0.55 else 0
            if registered:
                last_login = now - timedelta(days=RNG.randint(20, 100))
            else:
                last_login = None
        else:
            registered = 1 if RNG.random() < 0.7 else 0
            last_login = (
                now - timedelta(days=RNG.randint(95, 320)) if registered else None
            )

        product_mix = _assign_product_mix()
        rows.append(
            {
                "_segment": segment,
                "_product_mix": product_mix,
                "_investment_affinity": round(RNG.uniform(0.35, 1.15), 4),
                "_coverage_affinity": round(RNG.uniform(0.35, 1.15), 4),
                "_registered": registered,
                "CUSTOMER_ID": cid,
                "CUSTOMER_TYPE": customer_type,
                "FIRST_NAME": first,
                "SURNAME": last,
                "MARITAL_STATUS": material.marital_map[marital_code],
                "OCCUPATION": None if customer_type == "Organization" else RNG.choice(OCCUPATIONS),
                "EMAIL": email,
                "PHONE": mobile,
                "LAST_LOGIN": iso(last_login) if last_login else None,
                "CITY": city,
            }
        )

    return rows


def _pick_policy_status(segment: str, *, allow_inactive: bool) -> str:
    if segment == "engaged":
        return RNG.choice(POLICY_STATUS_ACTIVE)
    if segment == "at_risk" and (allow_inactive or RNG.random() < 0.55):
        return RNG.choice(POLICY_STATUS_INACTIVE)
    if segment == "stable" and allow_inactive and RNG.random() < 0.35:
        return RNG.choice(POLICY_STATUS_INACTIVE)
    return RNG.choices(
        POLICY_STATUS_ACTIVE,
        weights=[45, 40, 15],
        k=1,
    )[0]


def build_policies(customers: list[dict]) -> list[dict]:
    employers = require_seed_material().employers
    rows: list[dict] = []
    policy_seq = 100000

    active_customers = list(customers)
    history_months = iter_history_months()
    max_start_idx = max(0, len(history_months) - 1)

    for cust in active_customers:
        segment = cust.get("_segment", "engaged")
        if segment == "engaged":
            n_policies = RNG.randint(2, 4)
        elif segment == "stable":
            n_policies = RNG.randint(1, 3)
        else:
            n_policies = RNG.randint(1, 2)

        product_mix = cust.get("_product_mix", "balanced")
        if product_mix == "property_only":
            n_policies = max(1, min(n_policies, 2))
        elif product_mix == "pension_only":
            n_policies = max(1, min(n_policies, 3))
        type_codes = _policy_types_for_mix(product_mix, n_policies)
        type_by_code = {code: desc for code, desc in POLICY_TYPES}

        has_active = False
        prev_start: date | None = None
        for policy_idx in range(n_policies):
            policy_seq += 1
            ptype_code = type_codes[policy_idx] if policy_idx < len(type_codes) else type_codes[-1]
            ptype_desc = type_by_code.get(ptype_code, POLICY_TYPES[0][1])
            allow_inactive = policy_idx > 0 or segment != "engaged"
            status_desc = _pick_policy_status(segment, allow_inactive=allow_inactive)
            is_active = _in_force(status_desc)
            if is_active:
                has_active = True
            elif policy_idx == n_policies - 1 and not has_active and segment != "at_risk":
                status_desc = RNG.choice(POLICY_STATUS_ACTIVE)
                is_active = True
                has_active = True
            if policy_idx == 0:
                start_idx = RNG.randint(0, max_start_idx)
                sm = history_months[start_idx]
                start = date(sm.year, sm.month, RNG.randint(1, 28))
            else:
                assert prev_start is not None
                gap = RNG.randint(4, 20)
                start = prev_start + timedelta(days=gap * 30)
                if start > BOOK_AS_OF_DATE:
                    start = BOOK_AS_OF_DATE.replace(day=RNG.randint(1, 15))
            prev_start = start
            end = (
                date(2099, 12, 31)
                if is_active
                else min(
                    BOOK_AS_OF_DATE,
                    date(
                        RNG.randint(start.year, BOOK_AS_OF_DATE.year),
                        RNG.randint(1, 12),
                        28,
                    ),
                )
            )
            group_contract = ptype_code in (401, 402) and RNG.random() < 0.4
            employer_name = RNG.choice(employers)[1] if group_contract else None
            premium = round(RNG.uniform(150, 4500), 2) if ptype_code != 301 else round(RNG.uniform(80, 600), 2)

            rows.append(
                {
                    "POLICY_NUMBER": policy_seq,
                    "CUSTOMER_ID": int(cust["CUSTOMER_ID"]),
                    "PRODUCT_CODE": ptype_code,
                    "PRODUCT_NAME": ptype_desc,
                    "POLICY_STATUS": status_desc,
                    "INCEPTION_DATE": iso(start),
                    "EXPIRY_DATE": iso(end),
                    "GROSS_MONTHLY_PREMIUM": premium,
                    "CONTRACT_TYPE": "Group" if group_contract else "Individual",
                    "EMPLOYER_NAME": employer_name,
                }
            )
    return rows


def build_foreclosures(
    customers: list[dict],
    policies: list[dict],
    *,
    target_fraction: float = 0.018,
    min_targets: int = 6,
) -> list[dict]:
    fc_rows: list[dict] = []
    active = list(customers)
    at_risk = [c for c in active if c.get("_segment") == "at_risk"]
    stable = [c for c in active if c.get("_segment") == "stable"]
    engaged = [c for c in active if c.get("_segment") == "engaged"]
    target_count = max(min_targets, int(len(active) * target_fraction))
    target_count = min(target_count, len(active))

    targets: list[dict] = []
    if at_risk:
        targets.extend(RNG.sample(at_risk, min(len(at_risk), max(1, int(target_count * 0.55)))))
    remaining = target_count - len(targets)
    if remaining > 0 and stable:
        targets.extend(RNG.sample(stable, min(len(stable), max(0, int(target_count * 0.35)))))
    remaining = target_count - len(targets)
    if remaining > 0 and engaged:
        pool = [c for c in engaged if c not in targets]
        if pool:
            targets.extend(RNG.sample(pool, min(len(pool), remaining)))
    if len(targets) < min(target_count, len(active)):
        pool = [c for c in active if c not in targets]
        if pool:
            targets.extend(RNG.sample(pool, min(len(pool), target_count - len(targets))))

    used_numbers: set[int] = set()
    for cust in targets:
        fc_num = RNG.randint(100000, 999999)
        while fc_num in used_numbers:
            fc_num = RNG.randint(100000, 999999)
        used_numbers.add(fc_num)
        amount = round(RNG.uniform(5000, 250000), 2)
        fc_date = date(RNG.randint(2019, 2024), RNG.randint(1, 12), RNG.randint(1, 28))
        reg_date = fc_date + timedelta(days=RNG.randint(5, 45))
        cust_policies = [
            p
            for p in policies
            if p["CUSTOMER_ID"] == cust["CUSTOMER_ID"] and _in_force(p["POLICY_STATUS"])
        ]
        policy_number = RNG.choice(cust_policies)["POLICY_NUMBER"] if cust_policies else None

        fc_rows.append(
            {
                "FORECLOSURE_NUMBER": fc_num,
                "CUSTOMER_ID": int(cust["CUSTOMER_ID"]),
                "POLICY_NUMBER": policy_number,
                "FORECLOSURE_AMOUNT": amount,
                "FORECLOSURE_DATE": iso(fc_date),
                "REGISTERED_DATE": iso(reg_date),
                "PORTFOLIO_NUMBER": RNG.randint(1000, 9999),
            }
        )

    return fc_rows


def build_policy_investment_tracks(
    policies: list[dict],
    months: list[date],
    *,
    investment_macro: list[float] | None = None,
    customers_by_id: dict[int, dict] | None = None,
) -> list[dict]:
    funds = require_seed_material().funds
    rows: list[dict] = []
    invest_policies = [
        p for p in policies if p["PRODUCT_CODE"] != PROPERTY_POLICY_TYPE and _in_force(p["POLICY_STATUS"])
    ]
    macro = investment_macro or [1.0] * len(months)
    month_index = {m: i for i, m in enumerate(months)}

    for pol in invest_policies:
        cust = None
        if customers_by_id is not None:
            try:
                cust = customers_by_id.get(int(pol["CUSTOMER_ID"]))
            except (TypeError, ValueError):
                cust = None
        product_mix = (cust or {}).get("_product_mix", "balanced")
        inv_affinity = float((cust or {}).get("_investment_affinity", 1.0))

        # Insurance-heavy customers often carry life policies without rich investment history.
        if pol["PRODUCT_CODE"] in LIFE_HEALTH_POLICY_TYPES and product_mix == "insurance_focus":
            if RNG.random() < 0.62:
                continue
        if product_mix == "insurance_focus" and pol["PRODUCT_CODE"] in INVESTMENT_POLICY_TYPES:
            if RNG.random() < 0.35:
                continue

        company = _fund_company(pol["PRODUCT_CODE"])
        fund_choices = [f for f in funds if f[0] == company]
        if not fund_choices:
            fund_choices = funds[:2]
        tracks = RNG.sample(fund_choices, k=min(len(fund_choices), RNG.randint(1, 2)))

        pstart = parse_policy_start(pol)
        scale = inv_affinity * RNG.uniform(0.65, 1.35)
        if product_mix == "investment_focus" or product_mix == "pension_only":
            scale *= RNG.uniform(1.1, 1.8)
        rewards = RNG.uniform(15_000, 45_000) * scale
        comp = RNG.uniform(5_000, 25_000) * scale
        policy_vol = RNG.uniform(0.004, 0.022)

        for snap in months:
            if not snapshot_on_or_after_policy_start(pstart, snap):
                continue
            idx = month_index.get(snap, 0)
            macro_ret = macro[idx] if idx < len(macro) else 0.0
            idio = 1.0 + RNG.uniform(-policy_vol, policy_vol + 0.012)
            rewards *= (1.0 + macro_ret) * idio
            comp *= (1.0 + macro_ret * 0.85) * (1.0 + RNG.uniform(-policy_vol, policy_vol + 0.01))
            total = rewards + comp
            ytd_r = round(rewards * RNG.uniform(-0.08, 0.22), 2)
            ytd_t = round(ytd_r + comp * RNG.uniform(-0.05, 0.12), 2)

            for _track_id, (_, fund_id, _) in enumerate(tracks, start=1):
                split = 1 / len(tracks)
                rows.append(
                    {
                        "CUSTOMER_ID": int(pol["CUSTOMER_ID"]),
                        "POLICY_NUMBER": pol["POLICY_NUMBER"],
                        "FUND_ID": fund_id,
                        "SNAPSHOT_DATE": iso(last_day_of_month(snap.year, snap.month)),
                        "ACCOUNT_VALUE": round(total * split, 2),
                        "YTD_GAIN_LOSS": round(ytd_t * split, 2),
                    }
                )
    return rows


def build_market_tracks(months: list[date]) -> list[dict]:
    funds = require_seed_material().funds
    product_line = {1: "Life", 7: "Pension", 8: "Provident"}
    rows: list[dict] = []
    for snap in months:
        snap_d = last_day_of_month(snap.year, snap.month)
        for mng, fund_id, fund_name in funds:
            rows.append(
                {
                    "FUND_ID": fund_id,
                    "FUND_NAME": fund_name,
                    "SNAPSHOT_DATE": iso(snap_d),
                    "PRODUCT_LINE": product_line[mng],
                    "MONTHLY_YIELD": round(RNG.uniform(-2.5, 3.5), 4),
                    "YTD_YIELD": round(RNG.uniform(-5, 12), 4),
                    "YIELD_3Y": round(RNG.uniform(2, 10), 4),
                    "YIELD_5Y": round(RNG.uniform(3, 9), 4),
                    "SHARPE_RATIO": round(RNG.uniform(0.2, 1.5), 4),
                    "TOTAL_ASSETS": round(RNG.uniform(500_000_000, 12_000_000_000), 2),
                }
            )
    return rows


def build_policy_status_snapshots(
    policies: list[dict],
    months: list[date],
    *,
    insurance_macro: list[float] | None = None,
    customers_by_id: dict[int, dict] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    life_health = [p for p in policies if p["PRODUCT_CODE"] in LIFE_HEALTH_POLICY_TYPES]
    macro = insurance_macro or [1.0] * len(months)
    month_index = {m: i for i, m in enumerate(months)}

    for pol in life_health:
        cust = None
        if customers_by_id is not None:
            try:
                cust = customers_by_id.get(int(pol["CUSTOMER_ID"]))
            except (TypeError, ValueError):
                cust = None
        product_mix = (cust or {}).get("_product_mix", "balanced")
        cov_affinity = float((cust or {}).get("_coverage_affinity", 1.0))
        mix_scale = 1.0
        if product_mix == "insurance_focus":
            mix_scale = RNG.uniform(1.15, 1.75)
        elif product_mix in ("investment_focus", "pension_only"):
            mix_scale = RNG.uniform(0.55, 0.95)
        elif product_mix == "property_only":
            continue

        base_sum = RNG.uniform(200_000, 2_500_000) * cov_affinity * mix_scale
        sum_insured = base_sum
        savings_balance = base_sum * RNG.uniform(0.12, 0.28)
        surrender_value = base_sum * RNG.uniform(0.06, 0.18)
        premium = float(pol["GROSS_MONTHLY_PREMIUM"] or 0)
        premium_drift = RNG.uniform(-0.012, 0.018)
        cov_vol = RNG.uniform(0.003, 0.016)

        pstart = parse_policy_start(pol)
        for snap in months:
            if not snapshot_on_or_after_policy_start(pstart, snap):
                continue
            idx = month_index.get(snap, 0)
            macro_ret = macro[idx] if idx < len(macro) else 0.0
            ins_idio = 1.0 + RNG.uniform(-cov_vol, cov_vol + 0.008)
            sum_insured *= (1.0 + macro_ret * 0.35) * ins_idio
            savings_balance *= (1.0 + macro_ret * 0.55) * (1.0 + RNG.uniform(-cov_vol, cov_vol + 0.01))
            surrender_value *= (1.0 + macro_ret * 0.45) * (1.0 + RNG.uniform(-cov_vol, cov_vol))
            premium = max(50.0, premium * (1.0 + premium_drift + RNG.uniform(-0.01, 0.012)))

            snap_d = last_day_of_month(snap.year, snap.month)
            rows.append(
                {
                    "CUSTOMER_ID": int(pol["CUSTOMER_ID"]),
                    "POLICY_NUMBER": pol["POLICY_NUMBER"],
                    "SNAPSHOT_DATE": iso(snap_d),
                    "POLICY_STATUS": pol["POLICY_STATUS"],
                    "SUM_INSURED": round(sum_insured, 2),
                    "GROSS_MONTHLY_PREMIUM": round(premium, 2),
                    "DEATH_BENEFIT": round(sum_insured * RNG.uniform(0.88, 1.0), 2),
                    "SURRENDER_VALUE": round(surrender_value, 2),
                    "SAVINGS_BALANCE": round(savings_balance, 2),
                    "PAID_UP_VALUE": round(surrender_value * 0.9, 2),
                }
            )
    return rows


def insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict],
    *,
    chunk_size: int = 5000,
) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(cols)
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"
    for start in range(0, len(rows), chunk_size):
        batch = rows[start : start + chunk_size]
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in batch])


def init_database(
    db_path: Path,
    rebuild: bool = True,
    *,
    customer_count: int = DEFAULT_SEED_CUSTOMERS,
    territory_id: str | None = None,
    seed_language: str | None = None,
) -> None:
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    admin_backup = backup_admin_state(db_path) if rebuild and db_path.is_file() else None
    if rebuild and db_path.exists():
        db_path.unlink()

    resolved_territory = resolve_territory_id(territory_id=territory_id)
    territory = get_territory(resolved_territory)
    resolved_language = resolve_seed_language(
        seed_language=seed_language,
        territory_id=territory.id,
    )
    set_active_territory(territory)
    set_seed_language(resolved_language)
    material = resolve_seed_material(territory, resolved_language)
    set_seed_material(material)
    print(
        f"Seed region: {territory.display_name} ({territory.id}); "
        f"seed language: {resolved_language}; material: {material.source}",
        flush=True,
    )

    n = max(1, min(int(customer_count), MAX_SEED_CUSTOMERS))
    if n != customer_count:
        print(f"Note: customer_count clamped to {n} (max {MAX_SEED_CUSTOMERS})", flush=True)

    months = iter_history_months()
    print(
        f"History window: {months[0].strftime('%Y-%m')} → "
        f"{months[-1].strftime('%Y-%m')} ({len(months)} monthly snapshots)",
        flush=True,
    )

    print(f"Generating {n} representative customers…", flush=True)
    customers = build_customers(n)
    print("Building policies…", flush=True)
    policies = build_policies(customers)
    print("Building foreclosures, investments, coverage snapshots…", flush=True)
    foreclosures = build_foreclosures(customers, policies)
    investment_macro, insurance_macro = build_decorrelated_macro_series(months)
    customers_by_id = {int(c["CUSTOMER_ID"]): c for c in customers}
    pit = build_policy_investment_tracks(
        policies,
        months,
        investment_macro=investment_macro,
        customers_by_id=customers_by_id,
    )
    market = build_market_tracks(months)
    policy_status = build_policy_status_snapshots(
        policies,
        months,
        insurance_macro=insurance_macro,
        customers_by_id=customers_by_id,
    )
    print("Building interaction events…", flush=True)
    interactions = build_interaction_events(
        customers,
        policies,
        foreclosures,
        rng=RNG,
        interaction_copy=material.interaction_copy,
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(schema_path().read_text(encoding="utf-8"))
        if INTERACTIONS_SCHEMA.is_file():
            conn.executescript(INTERACTIONS_SCHEMA.read_text(encoding="utf-8"))
        churn_schema = project_root() / "data" / "churn_schema.sql"
        if churn_schema.is_file():
            conn.executescript(churn_schema.read_text(encoding="utf-8"))
        insights_schema = project_root() / "data" / "insights_schema.sql"
        if insights_schema.is_file():
            conn.executescript(insights_schema.read_text(encoding="utf-8"))
        metrics_schema = project_root() / "data" / "metrics_schema.sql"
        if metrics_schema.is_file():
            conn.executescript(metrics_schema.read_text(encoding="utf-8"))

        print("Writing warehouse tables…", flush=True)
        insert_rows(conn, "DIM_CUSTOMERS", _customer_rows_for_db(customers))
        insert_rows(conn, "DIM_POLICIES", _policy_rows_for_db(policies))
        insert_rows(conn, "FCT_POLICY_FORECLOSURES", foreclosures)
        insert_rows(conn, "FCT_POLICY_INVESTMENTS", pit)
        insert_rows(conn, "FCT_INVESTMENT_FUNDS", market)
        insert_rows(conn, "FCT_POLICY_STATUS", policy_status)
        insert_rows(conn, "APP_CUSTOMER_INTERACTION_EVENTS", interactions)
        write_territory_config(
            conn,
            territory,
            seed_language=resolved_language,
            city_names=list(material.canonical_cities()),
            city_aliases=material.city_aliases,
            seed_material_source=material.source,
        )
        conn.commit()

        from customer360.metrics_refresh import (
            refresh_customer_metrics,
            refresh_overview_counts,
            refresh_portfolio_analytics_cache,
        )

        print("Precomputing customer list metrics and overview counts…", flush=True)
        refresh_customer_metrics(conn)
        refresh_overview_counts(conn)
        from customer360.churn.scoring import try_train_and_score_churn

        if try_train_and_score_churn(db_path):
            print("Churn model scored and APP_CUSTOMER_CHURN_SCORES populated.", flush=True)
        else:
            print(
                "Churn scoring skipped (install ML deps or run train job); "
                "retention KPIs stay blank until scores exist.",
                flush=True,
            )
        print("Caching portfolio analytics (all segments)…", flush=True)
        refresh_portfolio_analytics_cache(conn)
        from customer360.api.warehouse_admin import refresh_warehouse_manifest

        refresh_warehouse_manifest(conn, source_job="seed")

        counts = conn.execute(
            """
            SELECT 'DIM_CUSTOMERS' AS tbl, COUNT(*) FROM DIM_CUSTOMERS
            UNION ALL SELECT 'DIM_POLICIES', COUNT(*) FROM DIM_POLICIES
            UNION ALL SELECT 'FCT_POLICY_FORECLOSURES', COUNT(*) FROM FCT_POLICY_FORECLOSURES
            UNION ALL SELECT 'FCT_POLICY_INVESTMENTS', COUNT(*) FROM FCT_POLICY_INVESTMENTS
            UNION ALL SELECT 'FCT_INVESTMENT_FUNDS', COUNT(*) FROM FCT_INVESTMENT_FUNDS
            UNION ALL SELECT 'FCT_POLICY_STATUS', COUNT(*) FROM FCT_POLICY_STATUS
            UNION ALL SELECT 'APP_CUSTOMER_INTERACTION_EVENTS', COUNT(*) FROM APP_CUSTOMER_INTERACTION_EVENTS
            """
        ).fetchall()

    admin_conn = sqlite3.connect(db_path)
    admin_conn.row_factory = sqlite3.Row
    ensure_admin_schema(admin_conn)
    if admin_backup:
        restore_admin_state(db_path, admin_backup)
    else:
        ensure_kpi_benchmark_catalog(admin_conn)
    admin_conn.commit()
    admin_conn.close()
    ensure_sqlite_warehouse_path_registered(db_path)

    print(f"Database written to {db_path}")
    for name, count in counts:
        print(f"  {name}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: CUSTOMER360_DB_PATH or data/customer360.db)",
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Append to existing DB without deleting (schema still applied)",
    )
    parser.add_argument(
        "--customers",
        type=int,
        default=DEFAULT_SEED_CUSTOMERS,
        metavar="N",
        help=f"Number of current customers to generate (max {MAX_SEED_CUSTOMERS}, default {DEFAULT_SEED_CUSTOMERS})",
    )
    parser.add_argument(
        "--territory",
        type=str,
        default=None,
        metavar="ID",
        help="Seed region profile: il, gb, es, sa, fr (default: CUSTOMER360_SEED_TERRITORY or il)",
    )
    parser.add_argument(
        "--language",
        type=str,
        default=None,
        metavar="ID",
        help="Seed language for interaction copy: en, fr, es, he, ar (env: CUSTOMER360_SEED_LANGUAGE)",
    )
    args = parser.parse_args()
    db_path = args.db or resolve_sqlite_warehouse_path()
    init_database(
        db_path,
        rebuild=not args.no_rebuild,
        customer_count=args.customers,
        territory_id=args.territory,
        seed_language=args.language,
    )


if __name__ == "__main__":
    main()
