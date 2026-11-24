-- Insurance Customer 360 warehouse.
-- Six subject tables, at most ten columns each.
-- Names follow common life / savings practice: party, contract, in-force valuation,
-- policy lien, unit-linked holding, and fund factsheet.

PRAGMA foreign_keys = ON;

-- Policyholder. The nine requested party attributes, plus CITY for rating territory.
CREATE TABLE IF NOT EXISTS DIM_CUSTOMERS (
    CUSTOMER_ID     INTEGER PRIMARY KEY,
    CUSTOMER_TYPE   TEXT    NOT NULL, -- Individual | Organization
    FIRST_NAME      TEXT,
    SURNAME         TEXT,
    MARITAL_STATUS  TEXT,             -- Single | Married | Divorced | Widowed
    OCCUPATION      TEXT,             -- Professional | Clerical | Manual | Self-employed | Retired | Student
    EMAIL           TEXT,
    PHONE           TEXT,
    LAST_LOGIN      TEXT,
    CITY            TEXT
);

CREATE INDEX IF NOT EXISTS idx_customers_city ON DIM_CUSTOMERS (CITY);

-- Insurance contract.
CREATE TABLE IF NOT EXISTS DIM_POLICIES (
    POLICY_NUMBER          INTEGER PRIMARY KEY,
    CUSTOMER_ID            INTEGER NOT NULL,
    PRODUCT_CODE           INTEGER NOT NULL,
    PRODUCT_NAME           TEXT    NOT NULL,
    POLICY_STATUS          TEXT    NOT NULL, -- Active | Premium Paying | Paid Up | Lapsed | Surrendered
    INCEPTION_DATE         TEXT,
    EXPIRY_DATE            TEXT,
    GROSS_MONTHLY_PREMIUM  REAL,
    CONTRACT_TYPE          TEXT    NOT NULL, -- Individual | Group
    EMPLOYER_NAME          TEXT,
    FOREIGN KEY (CUSTOMER_ID) REFERENCES DIM_CUSTOMERS (CUSTOMER_ID)
);

CREATE INDEX IF NOT EXISTS idx_policies_customer ON DIM_POLICIES (CUSTOMER_ID);
CREATE INDEX IF NOT EXISTS idx_policies_product ON DIM_POLICIES (PRODUCT_CODE, POLICY_STATUS);

-- In-force valuation snapshot (life and health).
CREATE TABLE IF NOT EXISTS FCT_POLICY_STATUS (
    CUSTOMER_ID             INTEGER NOT NULL,
    POLICY_NUMBER           INTEGER NOT NULL,
    SNAPSHOT_DATE           TEXT    NOT NULL,
    POLICY_STATUS           TEXT,
    SUM_INSURED             REAL,
    GROSS_MONTHLY_PREMIUM   REAL,
    DEATH_BENEFIT           REAL,
    SURRENDER_VALUE         REAL,
    SAVINGS_BALANCE         REAL,
    PAID_UP_VALUE           REAL,
    PRIMARY KEY (CUSTOMER_ID, POLICY_NUMBER, SNAPSHOT_DATE),
    FOREIGN KEY (POLICY_NUMBER) REFERENCES DIM_POLICIES (POLICY_NUMBER)
);

CREATE INDEX IF NOT EXISTS idx_policy_status_customer
    ON FCT_POLICY_STATUS (CUSTOMER_ID, SNAPSHOT_DATE);

-- Lien / encumbrance registered against a policy.
CREATE TABLE IF NOT EXISTS FCT_POLICY_FORECLOSURES (
    FORECLOSURE_NUMBER   INTEGER PRIMARY KEY,
    CUSTOMER_ID          INTEGER NOT NULL,
    POLICY_NUMBER        INTEGER,
    FORECLOSURE_AMOUNT   REAL,
    FORECLOSURE_DATE     TEXT,
    REGISTERED_DATE      TEXT,
    PORTFOLIO_NUMBER     INTEGER,
    FOREIGN KEY (CUSTOMER_ID) REFERENCES DIM_CUSTOMERS (CUSTOMER_ID),
    FOREIGN KEY (POLICY_NUMBER) REFERENCES DIM_POLICIES (POLICY_NUMBER)
);

CREATE INDEX IF NOT EXISTS idx_foreclosures_customer
    ON FCT_POLICY_FORECLOSURES (CUSTOMER_ID);

-- Unit-linked or pension holding on a policy, one row per fund per month.
CREATE TABLE IF NOT EXISTS FCT_POLICY_INVESTMENTS (
    CUSTOMER_ID      INTEGER NOT NULL,
    POLICY_NUMBER    INTEGER NOT NULL,
    FUND_ID          INTEGER NOT NULL,
    SNAPSHOT_DATE    TEXT    NOT NULL,
    ACCOUNT_VALUE    REAL,
    YTD_GAIN_LOSS    REAL,
    PRIMARY KEY (POLICY_NUMBER, FUND_ID, SNAPSHOT_DATE),
    FOREIGN KEY (POLICY_NUMBER) REFERENCES DIM_POLICIES (POLICY_NUMBER)
);

CREATE INDEX IF NOT EXISTS idx_investments_customer
    ON FCT_POLICY_INVESTMENTS (CUSTOMER_ID, SNAPSHOT_DATE);

-- Fund factsheet. Not customer-specific.
CREATE TABLE IF NOT EXISTS FCT_INVESTMENT_FUNDS (
    FUND_ID         INTEGER NOT NULL,
    FUND_NAME       TEXT,
    SNAPSHOT_DATE   TEXT    NOT NULL,
    PRODUCT_LINE    TEXT,             -- Life | Pension | Provident
    MONTHLY_YIELD   REAL,
    YTD_YIELD       REAL,
    YIELD_3Y        REAL,
    YIELD_5Y        REAL,
    SHARPE_RATIO    REAL,
    TOTAL_ASSETS    REAL,
    PRIMARY KEY (FUND_ID, SNAPSHOT_DATE)
);
