# Personal Finance Ingestion API

A production-grade REST API for ingesting, persisting, and analysing personal finance transactions.  
Built with **FastAPI**, **SQLAlchemy 2**, **Pydantic v2**, **Pandas**, and **Docker**.

---

## Features

- 📥 **Dual ingestion modes** – JSON payload or CSV bank-statement upload (multipart)
- 🔁 **Duplicate detection** – SHA-256 fingerprint prevents re-ingesting the same transaction
- 📊 **Analytics** – Category and month-level spending summaries
- 🔍 **Flexible querying** – Pagination, date-range, category, and account filters
- 🐳 **Containerised** – Multi-stage Dockerfile + PostgreSQL via Docker Compose
- 🧪 **Fully tested** – 20+ pytest integration tests with an in-memory SQLite DB

---

## Project Structure

```
finance_api/
├── app/
│   ├── main.py          # App factory, lifespan, global exception handlers
│   ├── config.py        # Pydantic Settings (env vars + .env file)
│   ├── database.py      # SQLAlchemy engine, session, Base
│   ├── models.py        # ORM models: Account, Transaction
│   ├── schemas.py       # Pydantic schemas (request/response)
│   ├── crud.py          # DB access layer
│   └── routers/
│       ├── ingest.py        # POST /ingest/json, POST /ingest/csv, /accounts
│       └── transactions.py  # GET /transactions, GET /summary
├── tests/
│   └── test_api.py      # Pytest integration tests (SQLite in-memory)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env
```

---

## Quick Start

### Local development (SQLite)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the server (SQLite, auto-creates finance.db)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000/docs** for the interactive Swagger UI.

---

### Docker Compose (PostgreSQL)

```bash
# 1. Copy and customise the environment file
cp .env .env.local
# Edit .env.local – change POSTGRES_PASSWORD at minimum

# 2. Build and start all services
docker compose --env-file .env.local up --build -d

# 3. Tail logs
docker compose logs -f api
```

Services:
| Service | Port |
|---------|------|
| FastAPI API | `http://localhost:8000` |
| PostgreSQL  | `localhost:5432`        |

---

## API Reference

### Accounts

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/accounts` | Create an account |
| `GET`  | `/api/v1/accounts` | List all accounts |

### Ingestion

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/ingest/json` | Bulk ingest JSON transactions |
| `POST` | `/api/v1/ingest/csv`  | Upload a CSV bank statement |

### Transactions

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/v1/transactions` | Paginated transaction list |
| `GET`  | `/api/v1/summary`      | Category & month summaries |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/health` | Liveness probe |

---

## CSV Format

The minimum required columns are `date`, `amount`, and `description`.  
Column names are **case-insensitive** and many common aliases are supported:

| Canonical | Accepted aliases |
|-----------|-----------------|
| `date` | `Transaction Date`, `Trans Date`, `Posting Date`, `Value Date` |
| `amount` | `Debit`, `Credit`, `Transaction Amount` |
| `description` | `Narrative`, `Details`, `Memo`, `Particulars`, `Reference` |
| `merchant` | `Payee`, `Vendor` |
| `category` | `Type`, `Tag` |

**Example CSV:**

```csv
date,amount,description,merchant,category,currency
2024-03-15,-45.99,Weekly groceries,Whole Foods,Groceries,USD
2024-03-16,-12.50,Morning coffee,Starbucks,Dining,USD
2024-03-17,2000.00,Monthly salary,Employer Ltd,Income,USD
```

---

## Running Tests

```bash
# From the project root
pytest tests/ -v

# With coverage
pip install pytest-cov
pytest tests/ -v --cov=app --cov-report=term-missing
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///./finance.db` | SQLAlchemy DSN |
| `APP_ENV` | `development` | `development` / `testing` / `production` |
| `DEBUG` | `false` | Enable SQLAlchemy query logging |
| `MAX_CSV_ROWS` | `50000` | Maximum rows per CSV upload |
| `MAX_UPLOAD_SIZE_MB` | `10` | Maximum CSV file size in MB |
| `POSTGRES_DB` | `finance` | PostgreSQL database name (Compose) |
| `POSTGRES_USER` | `financeuser` | PostgreSQL user (Compose) |
| `POSTGRES_PASSWORD` | *(required)* | PostgreSQL password (Compose) |

---

## Duplicate Detection

Each transaction is fingerprinted with a SHA-256 hash derived from:

```
SHA256( date | amount | description.lower() | merchant.lower() | account_id )
```

Both intra-payload duplicates and database duplicates are detected before any insert,
making ingestion **idempotent** and safe to retry.

---

## Architecture Notes

- **`app/crud.py`** – all DB mutations live here; routers are kept thin.
- **`app/database.py`** – engine is built once; `get_db()` is a FastAPI dependency.
- **`app/main.py`** – global exception handlers for `ValidationError`, `IntegrityError`,
  `OperationalError`, and uncaught exceptions ensure clean JSON error responses.
- **Dockerfile** – multi-stage build; the runtime image has no build tools and runs as a
  non-root user (`appuser`).
