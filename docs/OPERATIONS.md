# Insurance Customer 360 — Operations Guide

Configuration, data, and maintenance details for running the blueprint. For first-time
deployment on Cloudera AI, start with [CAI_APPLICATION.md](CAI_APPLICATION.md).

## Environment variables

| Variable                         | Default               | Purpose                                                                                                             |
| -------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `CUSTOMER360_DB_PATH`            | `data/customer360.db` | Bootstrap SQLite path for admin settings (set to your warehouse file in CAI, e.g. `/home/cdsw/data/customer360.db`) |
| `CUSTOMER360_SEED_TERRITORY`     | `il`                  | Demo **region** for seed data: `il`, `gb`, `es`, `sa`, `fr` (names, cities, currency)                               |
| `CUSTOMER360_SEED_LANGUAGE`      | (per region)          | **Seed language** for interaction copy: `en`, `fr`, `es`, `he`, `ar` (independent of region)                        |
| `CUSTOMER360_SEED_CUSTOMERS`     | `2000`                | Customer count when seeding                                                                                         |
| `CUSTOMER360_SEED_SKIP_LLM`      | `0`                   | `1` skips LLM-generated seed material (offline / tests)                                                             |
| `CUSTOMER360_SEED_LLM_REFRESH`   | `0`                   | `1` bypasses the seed material cache                                                                                |
| `CUSTOMER360_SEED_REQUIRE_LLM`   | `0`                   | `1` fails the seed if LLM generation is unavailable                                                                 |
| `AWS_*`, `CUSTOMER360_BEDROCK_*` | —                     | Amazon Bedrock credentials, region, and model; see [`.env.example`](../.env.example)                                |

Application scripts bind to `127.0.0.1` and use `CDSW_APP_PORT` (Workbench/CML) or `APP_PORT`
(AI Inference, default 8080). Do not hard-code ports.

## LLM providers

The LLM provider is selected in **Admin → Configuration → LLM provider**:

- **Amazon Bedrock** (default) — configured through `AWS_*` and `CUSTOMER360_BEDROCK_*`.
- **PrivateAI / OpenAI-compatible** — any OpenAI-style HTTP endpoint (for example Cloudera AI
  Inference, vLLM, or Ollama). The API token is stored in the local admin database and is never
  returned by the API.

When no provider is reachable, insights fall back to rule-based summaries.

## Region, seed language, and UI localization

**Region** profiles drive warehouse geography (customer names, cities, employers, currency) and
LLM market context. **Seed language** controls interaction event text and is stored separately
(`APP_TERRITORY_CONFIG.SEED_LANGUAGE`). UI languages are `en`, `fr`, `es`, `he`, and `ar`
(Hebrew is not the same as the Israel region id `il`).

**Admin → Configuration → Localization and Territory** persists UI region override and preferred
UI language in `APP_ADMIN_LOCALIZATION` (without reseeding). Use **Reseed warehouse** in that
panel or `POST /api/admin/warehouse/reseed` to rebuild data for a new region + seed language.

Reseed examples:

```bash
CUSTOMER360_SEED_TERRITORY=il CUSTOMER360_SEED_LANGUAGE=en python3 scripts/init_db.py
# or
python3 -m customer360.seed --territory sa --language ar --customers 500
```

### LLM seed material

When an LLM provider is configured, `init_database` / reseed calls the model with representative
samples for the selected region and seed language, validates the JSON, writes customers and
interaction copy from that bundle, and caches it under `data/seed_cache/{region}_{language}.json`.

### Translating the UI

After editing `frontend/src/locales/en.json`, regenerate non-English packs and rebuild the UI:

```bash
python3 -m pip install -r requirements.txt   # includes deep-translator
cd frontend && python3 scripts/build-language-packs.py
npm run build   # updates frontend/dist bundled with Vite
```

Use `{lang}-patch.json` to fix machine-translation glitches (tab labels, product names).
Set `CUSTOMER360_I18N_SKIP_TRANSLATE=1` only for offline smoke tests (overlay-only packs).

## Warehouse domains

| Table                             | Domain                                                 |
| --------------------------------- | ------------------------------------------------------ |
| `DIM_CUSTOMERS`                   | Policyholder party (identity, contact, city, login)    |
| `DIM_POLICIES`                    | Life, health, property, pension, and savings contracts |
| `FCT_POLICY_FORECLOSURES`         | Liens registered against a policy                      |
| `FCT_POLICY_INVESTMENTS`          | Monthly account value by policy and fund               |
| `FCT_INVESTMENT_FUNDS`            | Fund factsheet yields and assets                       |
| `FCT_POLICY_STATUS`               | In-force valuation: sum insured, surrender, savings    |
| `APP_CUSTOMER_CHURN_SCORES`       | Churn probability and risk tier per customer ID        |
| `APP_CUSTOMER_INTERACTION_EVENTS` | Synthetic reviews, agent questions, and web searches   |
| `APP_CUSTOMER_AI_INSIGHTS`        | Cached LLM (or fallback) customer summaries            |

DDL lives in [`data/schema.sql`](../data/schema.sql) and the other `data/*_schema.sql` files.
Generated databases are gitignored (`data/*.db`).

## Churn model

Features are built from warehouse behaviour plus interaction events (reviews, agent questions,
product/help searches), normalized with `StandardScaler`, and modeled with balanced logistic
regression. Scores are stored in SQLite for the API and UI. Retrain after reseeding:

```bash
python3 3_job-train-churn-model/train_churn.py
```

## Scaling to ~5,000 customers

The seed generator scales every table (policies, investments, coverage snapshots, foreclosures,
interactions) from the same logic as the default warehouse:

```bash
python3 scripts/scale_warehouse.py --customers 5000
```

Or set `CUSTOMER360_SEED_CUSTOMERS=5000` before the init job /
`python3 -m customer360.seed --customers 5000`, then rerun the churn training job.

## API performance (large warehouses)

After seeding, the warehouse precomputes:

- `APP_CUSTOMER_METRICS` — per-customer value, policy count, investment tracks.
- `APP_OVERVIEW_COUNTS` — domain card counts.
- `APP_PORTFOLIO_ANALYTICS_CACHE` — portfolio charts/KPIs per segment. Cache misses are computed
  once and stored (write-through).
- `APP_BOOK_*_TREND` — materialized book-wide objective series (savings AUM, premium momentum,
  engagement).

Refresh caches on an existing DB without reseeding:

```bash
python3 scripts/refresh_api_caches.py
```

The customer list API enforces `limit` **≤ 100**; the UI defaults to 50 with 25/50/100 options.

## Frontend build

The repo ships a prebuilt `frontend/dist/`. Restart the application after `git pull` — you do
**not** need `npm` for that. Rebuild only when you change the UI:

```bash
cd frontend && npm install && npm run build
```

To rebuild in a standard Python 3.11 CAI session (no system Node):

```python
%run scripts/build_frontend.py
```

The script skips work if `frontend/dist/index.html` already exists. It downloads a portable
Node.js binary into `.tools/` when `npm` is missing (requires outbound HTTPS to `nodejs.org`).

For UI development, `npm run dev` starts Vite on port 5173 and proxies `/api` to
`http://127.0.0.1:8080`.

## Workbench interactive runs (`__file__`)

`Path(__file__)` is only defined when Python executes a **file**. In an interactive Workbench
cell, pasting lines that use `__file__` raises `NameError`. Use one of these instead:

```python
# From a session whose working directory is the project root:
%run 2_job-init-database/init_database.py
%run 3_job-train-churn-model/train_churn.py   # after database init
```

```python
import importlib.util
from pathlib import Path

for candidate in (Path.cwd(), *Path.cwd().parents):
    boot = candidate / "bootstrap_entry.py"
    if boot.is_file():
        spec = importlib.util.spec_from_file_location("bootstrap_entry", boot)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.bootstrap(None)
        break

from customer360.paths import default_db_path
from customer360.seed import init_database

init_database(default_db_path(), rebuild=True)
```

If install fails with a missing `requirements.txt`, run:

```python
%run 1_session-install-dependencies/install.py
```

## Applied AI sentiment reference service

A separate, self-contained demo (TF-IDF + logistic regression via scikit-learn) served through
FastAPI with a static web UI. It is not part of the Customer 360 blueprint; its dependencies
are covered by the root `requirements.txt`.

```
app/              FastAPI app + model training/inference
scripts/train.py  Trains models/sentiment.joblib
static/index.html Demo UI
```

```bash
pip install -r requirements.txt
python -m scripts.train
uvicorn app.main:app --host 0.0.0.0 --port 8000   # http://localhost:8000
```
