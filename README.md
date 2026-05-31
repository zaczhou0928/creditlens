# CreditLens

A retail credit analytics assistant built with **FastAPI**, **Vue 3**, **LangGraph**, **MCP**, and **DuckDB**. Ask questions about loan applications, approvals, disbursements, repayments, and overdue balances, then inspect the SQL and supporting results.

CreditLens uses reproducible synthetic data. It analyzes portfolio operations; it does not make lending decisions, score borrowers, or predict defaults.

## Capabilities

- Retrieve authorized schema fields with BM25, embeddings, reciprocal rank fusion, and reranking.
- Execute model-generated SQL through bounded MCP tool calls and a read-only query engine.
- Enforce institution and field access before SQL execution, including nested queries.
- Preserve clarification state, query ownership, data versions, and result fingerprints.
- Build analyses and charts from server-stored results with checked fact references and decimal calculations.

## Quick start

Requires **Python 3.13** and **Node.js 22.12+**. The UI and demo questions are in Chinese. Demo mode requires no model key or external data service.

From the repository root on macOS or Linux:

```sh
python3.13 -m venv backend/.venv-local
backend/.venv-local/bin/python -m pip install -r backend/requirements-dev.txt -c backend/constraints.txt
cp backend/.env.example backend/.env
cd frontend
npm ci
cd ..
./start
```

Copy the example configuration only for a fresh setup. The API generates the synthetic dataset on first startup. Open the [workspace](http://127.0.0.1:5173) or [API documentation](http://127.0.0.1:8000/docs).

| Demo login | Password | Scope |
| --- | --- | --- |
| `analyst` | `analyst123` | All institutions; customer names and phone numbers hidden |
| `east` | `east123` | Shanghai and Hangzhou; sensitive fields hidden |
| `risk` | `risk123` | Portfolio and overdue metrics; sensitive fields hidden |
| `admin` | `admin123` | All fields and schema-index administration |

These are public local-demo accounts. Tokens expire when the backend restarts. Start with a built-in query, follow up with a different metric or grouping, then select results for a report.

## Data and query modes

The generator creates 12 tables covering **2025-01-01 through 2026-09-30**. The demo reference date is **2026-10-01**, so “last month” means September. CSV files, indexes, and session databases are generated locally and excluded from Git.

- **Demo:** deterministic Chinese rules, 13 defined metrics, compiled SQL, and template reports.
- **Model:** request interpretation, schema retrieval, open SQL generation, clarification, and grounded analysis.

To use model mode, configure the Chat, Embedding, and Rerank endpoints in `backend/.env`, then set `CREDIT_DEMO_MODE=false` and `CREDIT_EXTERNAL_MODEL_ENABLED=true`. This sends questions, schema metadata, context, and authorized synthetic results to your configured provider and may incur charges. Keep credentials out of version control.

## Validation

From `backend` with the virtual environment installed:

```sh
.venv-local/bin/python seed_demo_data.py
CREDIT_DEMO_MODE=true CREDIT_EXTERNAL_MODEL_ENABLED=false .venv-local/bin/python -m unittest discover -s tests -v
.venv-local/bin/python scripts/validate_credit_data.py
.venv-local/bin/python scripts/evaluate_credit.py
.venv-local/bin/python scripts/live_smoke.py --local-contract
PYTHONPATH=. .venv-local/bin/python scripts/verify_benchmark_gold.py
```

Seeding replaces local synthetic CSV data. Run `npm run build` from `frontend` to type-check and build the UI.

Local validation passed **64 tests**, **120 metric cases**, **14 scripted open-query cases**, and **40 independent reference checks**. These verify engineering behavior, not model accuracy. The recorded live-model baseline still has semantic, clarification, and report-routing failures; see [validation](docs/validation.md).

## Documentation

- [Architecture and code analysis](docs/architecture.md)
- [Development stages](docs/development-history.md)
- [Validation and model limitations](docs/validation.md)
- [Evaluation guide](backend/evaluation/credit_v1/README.md)

The January–May 2026 commit dates are a retrospective development sequence, not actual implementation dates. Dependencies and synthetic-data dates reflect the prepared release, not historical package availability.
