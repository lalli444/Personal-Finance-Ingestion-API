"""
tests/test_api.py
-----------------
Comprehensive integration tests for the Finance Ingestion API.

Test strategy:
    - An in-memory SQLite database is created fresh for every test function
      via the ``db_session`` fixture, ensuring complete isolation.
    - The TestClient overrides the ``get_db`` dependency to use that session.
    - Tests cover: account CRUD, JSON/CSV ingestion, duplicate detection,
      schema validation, pagination, date filtering, and the summary endpoint.
"""

from __future__ import annotations

import os

os.environ["APP_ENV"] = "testing"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import io
import textwrap
from datetime import date, timedelta
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

# ── Fixtures ──────────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite://"  # in-memory, ephemeral per engine


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Create a fresh in-memory SQLite database for each test."""
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture(scope="function")
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Return a TestClient wired to the isolated in-memory session."""

    def _override_get_db() -> Generator[Session, None, None]:
        try:
            yield db_session
        finally:
            pass  # session lifecycle managed by db_session fixture

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_account(client: TestClient, **kwargs) -> dict:
    """Helper: create an account and assert 201."""
    payload = {
        "account_name": "Test Checking",
        "institution": "Test Bank",
        "account_type": "checking",
        **kwargs,
    }
    resp = client.post("/api/v1/accounts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _build_csv(rows: list[dict]) -> bytes:
    """Build a minimal CSV from a list of row dicts."""
    header = "date,amount,description,merchant,category,currency"
    lines = [header]
    for r in rows:
        lines.append(
            f"{r.get('date','2024-01-01')},"
            f"{r.get('amount','-10.00')},"
            f"{r.get('description','Test')},"
            f"{r.get('merchant','')},"
            f"{r.get('category','')},"
            f"{r.get('currency','USD')}"
        )
    return "\n".join(lines).encode()


# ── Account tests ─────────────────────────────────────────────────────────────


class TestAccounts:
    def test_create_account_success(self, client: TestClient) -> None:
        """A valid account creation returns 201 with all fields populated."""
        account = _create_account(client)
        assert account["account_name"] == "Test Checking"
        assert account["institution"] == "Test Bank"
        assert account["account_type"] == "checking"
        assert "id" in account
        assert "created_at" in account

    def test_create_account_invalid_type(self, client: TestClient) -> None:
        """An invalid account_type triggers a 422 validation error."""
        resp = client.post(
            "/api/v1/accounts",
            json={"account_name": "x", "institution": "y", "account_type": "INVALID"},
        )
        assert resp.status_code == 422

    def test_list_accounts_empty(self, client: TestClient) -> None:
        """Listing accounts on a fresh DB returns an empty list."""
        resp = client.get("/api/v1/accounts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_accounts_returns_created(self, client: TestClient) -> None:
        """Accounts created via POST appear in the GET list."""
        _create_account(client, account_name="A1")
        _create_account(client, account_name="A2")
        resp = client.get("/api/v1/accounts")
        assert resp.status_code == 200
        names = [a["account_name"] for a in resp.json()]
        assert "A1" in names
        assert "A2" in names


# ── JSON Ingestion tests ───────────────────────────────────────────────────────


class TestJsonIngestion:
    def test_ingest_json_success(self, client: TestClient) -> None:
        """A valid JSON payload inserts all transactions and reports success."""
        account = _create_account(client)
        payload = {
            "account_id": account["id"],
            "transactions": [
                {
                    "date": "2024-03-15",
                    "amount": -45.99,
                    "currency": "USD",
                    "description": "Supermarket purchase",
                    "category": "Groceries",
                    "merchant": "Whole Foods",
                    "account_id": account["id"],
                },
                {
                    "date": "2024-03-16",
                    "amount": -12.50,
                    "currency": "USD",
                    "description": "Coffee shop",
                    "category": "Dining",
                    "merchant": "Starbucks",
                    "account_id": account["id"],
                },
            ],
        }
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_received"] == 2
        assert data["inserted"] == 2
        assert data["duplicates_skipped"] == 0
        assert data["failed"] == 0

    def test_ingest_json_duplicate_detection(self, client: TestClient) -> None:
        """Re-ingesting the same transactions skips them as duplicates."""
        account = _create_account(client)
        tx = {
            "date": "2024-04-01",
            "amount": -100.00,
            "currency": "USD",
            "description": "Rent payment",
            "category": "Housing",
            "merchant": "Landlord LLC",
            "account_id": account["id"],
        }
        payload = {"account_id": account["id"], "transactions": [tx]}

        resp1 = client.post("/api/v1/ingest/json", json=payload)
        assert resp1.json()["inserted"] == 1

        resp2 = client.post("/api/v1/ingest/json", json=payload)
        data2 = resp2.json()
        assert data2["inserted"] == 0
        assert data2["duplicates_skipped"] == 1

    def test_ingest_json_duplicate_within_payload(self, client: TestClient) -> None:
        """Duplicates within the same request payload are collapsed to one insert."""
        account = _create_account(client)
        tx = {
            "date": "2024-05-10",
            "amount": -20.00,
            "currency": "USD",
            "description": "Pizza",
            "category": "Dining",
            "merchant": "Domino's",
            "account_id": account["id"],
        }
        payload = {"account_id": account["id"], "transactions": [tx, tx]}
        resp = client.post("/api/v1/ingest/json", json=payload)
        data = resp.json()
        assert data["inserted"] == 1
        assert data["duplicates_skipped"] == 1

    def test_ingest_json_unknown_account(self, client: TestClient) -> None:
        """Ingesting to a non-existent account returns 404."""
        payload = {
            "account_id": "00000000-0000-0000-0000-000000000000",
            "transactions": [
                {
                    "date": "2024-01-01",
                    "amount": -5.0,
                    "currency": "USD",
                    "description": "test",
                    "account_id": "00000000-0000-0000-0000-000000000000",
                }
            ],
        }
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.status_code == 404

    def test_ingest_json_empty_transactions_list(self, client: TestClient) -> None:
        """An empty transactions list is rejected with 422."""
        account = _create_account(client)
        payload = {"account_id": account["id"], "transactions": []}
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.status_code == 422

    def test_ingest_json_invalid_currency(self, client: TestClient) -> None:
        """A currency code that is not 3 uppercase letters triggers 422."""
        account = _create_account(client)
        payload = {
            "account_id": account["id"],
            "transactions": [
                {
                    "date": "2024-01-01",
                    "amount": -10.0,
                    "currency": "us",  # invalid – too short
                    "description": "test",
                    "account_id": account["id"],
                }
            ],
        }
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.status_code == 422

    def test_ingest_json_invalid_date(self, client: TestClient) -> None:
        """A malformed date string triggers a 422 validation error."""
        account = _create_account(client)
        payload = {
            "account_id": account["id"],
            "transactions": [
                {
                    "date": "not-a-date",
                    "amount": -10.0,
                    "currency": "USD",
                    "description": "test",
                    "account_id": account["id"],
                }
            ],
        }
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.status_code == 422


# ── CSV Ingestion tests ────────────────────────────────────────────────────────


class TestCsvIngestion:
    def test_ingest_csv_success(self, client: TestClient) -> None:
        """A valid CSV file ingests all rows correctly."""
        account = _create_account(client)
        csv_data = _build_csv(
            [
                {"date": "2024-06-01", "amount": "-55.00", "description": "Gym membership",
                 "merchant": "PureGym", "category": "Health"},
                {"date": "2024-06-02", "amount": "-30.00", "description": "Taxi ride",
                 "merchant": "Uber", "category": "Transport"},
            ]
        )
        resp = client.post(
            "/api/v1/ingest/csv",
            data={"account_id": account["id"]},
            files={"file": ("statement.csv", io.BytesIO(csv_data), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_received"] == 2
        assert data["inserted"] == 2
        assert data["failed"] == 0

    def test_ingest_csv_duplicate_detection(self, client: TestClient) -> None:
        """Re-uploading the same CSV skips all rows as duplicates."""
        account = _create_account(client)
        csv_data = _build_csv(
            [{"date": "2024-07-01", "amount": "-9.99", "description": "Streaming service",
              "merchant": "Netflix", "category": "Entertainment"}]
        )

        for _ in range(2):
            resp = client.post(
                "/api/v1/ingest/csv",
                data={"account_id": account["id"]},
                files={"file": ("stmt.csv", io.BytesIO(csv_data), "text/csv")},
            )
            assert resp.status_code == 200

        data = resp.json()
        assert data["inserted"] == 0
        assert data["duplicates_skipped"] == 1

    def test_ingest_csv_missing_required_column(self, client: TestClient) -> None:
        """A CSV missing the 'amount' column is rejected with 400."""
        account = _create_account(client)
        csv_bytes = b"date,description\n2024-01-01,Coffee\n"
        resp = client.post(
            "/api/v1/ingest/csv",
            data={"account_id": account["id"]},
            files={"file": ("bad.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 400
        assert "amount" in resp.json()["detail"].lower() or "missing" in resp.json()["detail"].lower()

    def test_ingest_csv_bad_amount_row_fails_gracefully(self, client: TestClient) -> None:
        """A row with a non-numeric amount is recorded as a failed row, not a crash."""
        account = _create_account(client)
        csv_bytes = b"date,amount,description\n2024-01-01,NOT_A_NUMBER,Coffee\n2024-01-02,-5.00,Tea\n"
        resp = client.post(
            "/api/v1/ingest/csv",
            data={"account_id": account["id"]},
            files={"file": ("mixed.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["inserted"] == 1
        assert data["failed"] == 1
        assert data["failed_rows"][0]["row_index"] == 0

    def test_ingest_csv_unknown_account(self, client: TestClient) -> None:
        """Uploading a CSV to a non-existent account returns 404."""
        csv_data = _build_csv([{"date": "2024-01-01", "amount": "-5.00", "description": "x"}])
        resp = client.post(
            "/api/v1/ingest/csv",
            data={"account_id": "does-not-exist"},
            files={"file": ("stmt.csv", io.BytesIO(csv_data), "text/csv")},
        )
        assert resp.status_code == 404

    def test_ingest_csv_column_aliases(self, client: TestClient) -> None:
        """CSVs using column aliases (e.g. 'Memo', 'Payee') are parsed correctly."""
        account = _create_account(client)
        # Use alias columns: "Transaction Date" -> date, "Memo" -> description, "Payee" -> merchant
        csv_bytes = (
            b"Transaction Date,amount,Memo,Payee,category,currency\n"
            b"2024-08-15,-25.00,Monthly subscription,Spotify,Entertainment,USD\n"
        )
        resp = client.post(
            "/api/v1/ingest/csv",
            data={"account_id": account["id"]},
            files={"file": ("alias.csv", io.BytesIO(csv_bytes), "text/csv")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["inserted"] == 1


# ── Transaction Query tests ────────────────────────────────────────────────────


class TestTransactionQueries:
    def _seed(self, client: TestClient, account_id: str) -> None:
        """Insert 5 transactions across different dates and categories."""
        txns = [
            {"date": "2024-01-10", "amount": -50.0, "description": "Supermarket",
             "merchant": "Tesco", "category": "Groceries"},
            {"date": "2024-01-20", "amount": -15.0, "description": "Coffee",
             "merchant": "Costa", "category": "Dining"},
            {"date": "2024-02-05", "amount": -200.0, "description": "Electric bill",
             "merchant": "EDF", "category": "Utilities"},
            {"date": "2024-02-18", "amount": -80.0, "description": "Dinner out",
             "merchant": "Nando's", "category": "Dining"},
            {"date": "2024-03-01", "amount": 2000.0, "description": "Salary",
             "merchant": "Employer Ltd", "category": "Income"},
        ]
        payload = {
            "account_id": account_id,
            "transactions": [
                {**tx, "currency": "GBP", "account_id": account_id} for tx in txns
            ],
        }
        resp = client.post("/api/v1/ingest/json", json=payload)
        assert resp.json()["inserted"] == 5

    def test_list_all_transactions(self, client: TestClient) -> None:
        """GET /transactions returns all seeded transactions."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/transactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5

    def test_filter_by_category(self, client: TestClient) -> None:
        """Category filter returns only transactions in that category."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/transactions", params={"category": "Dining"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert all(item["category"] == "Dining" for item in data["items"])

    def test_filter_by_date_range(self, client: TestClient) -> None:
        """Date range filter returns only transactions within the range."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get(
            "/api/v1/transactions",
            params={"date_from": "2024-02-01", "date_to": "2024-02-28"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_filter_invalid_date_range(self, client: TestClient) -> None:
        """date_from after date_to returns 422."""
        resp = client.get(
            "/api/v1/transactions",
            params={"date_from": "2024-12-01", "date_to": "2024-01-01"},
        )
        assert resp.status_code == 422

    def test_pagination(self, client: TestClient) -> None:
        """Pagination correctly slices results."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/transactions", params={"page": 1, "page_size": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["pages"] == 3

    def test_filter_by_account_id(self, client: TestClient) -> None:
        """Account ID filter scopes results to the correct account."""
        acc1 = _create_account(client, account_name="Account 1")
        acc2 = _create_account(client, account_name="Account 2")
        self._seed(client, acc1["id"])

        # Only acc2 transactions
        resp = client.get("/api/v1/transactions", params={"account_id": acc2["id"]})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # acc1 transactions
        resp = client.get("/api/v1/transactions", params={"account_id": acc1["id"]})
        assert resp.json()["total"] == 5


# ── Summary / Analytics tests ──────────────────────────────────────────────────


class TestSummaryEndpoint:
    def _seed(self, client: TestClient, account_id: str) -> None:
        txns = [
            {"date": "2024-01-10", "amount": -50.0, "description": "Supermarket",
             "merchant": "Tesco", "category": "Groceries"},
            {"date": "2024-01-20", "amount": -15.0, "description": "Coffee",
             "merchant": "Costa", "category": "Dining"},
            {"date": "2024-02-05", "amount": -200.0, "description": "Electric",
             "merchant": "EDF", "category": "Utilities"},
            {"date": "2024-02-18", "amount": -80.0, "description": "Restaurant",
             "merchant": "Nando's", "category": "Dining"},
        ]
        payload = {
            "account_id": account_id,
            "transactions": [
                {**tx, "currency": "GBP", "account_id": account_id} for tx in txns
            ],
        }
        client.post("/api/v1/ingest/json", json=payload)

    def test_summary_overall_totals(self, client: TestClient) -> None:
        """Summary overall total matches sum of all transaction amounts."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["overall_count"] == 4
        assert abs(data["overall_total"] - (-345.0)) < 0.01

    def test_summary_by_category(self, client: TestClient) -> None:
        """Summary by_category contains expected categories."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/summary")
        data = resp.json()
        cats = {c["category"]: c["total_amount"] for c in data["by_category"]}
        assert "Groceries" in cats
        assert "Dining" in cats
        assert "Utilities" in cats
        assert abs(cats["Dining"] - (-95.0)) < 0.01

    def test_summary_by_month(self, client: TestClient) -> None:
        """Summary by_month groups transactions into correct months."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get("/api/v1/summary")
        data = resp.json()
        months = {(m["year"], m["month"]): m for m in data["by_month"]}
        assert (2024, 1) in months
        assert (2024, 2) in months
        assert months[(2024, 1)]["transaction_count"] == 2

    def test_summary_scoped_to_account(self, client: TestClient) -> None:
        """Summary with account_id filter scopes results to that account."""
        acc1 = _create_account(client, account_name="Acc1")
        acc2 = _create_account(client, account_name="Acc2")
        self._seed(client, acc1["id"])

        resp = client.get("/api/v1/summary", params={"account_id": acc2["id"]})
        data = resp.json()
        assert data["overall_count"] == 0
        assert data["overall_total"] == 0.0

    def test_summary_date_range_filter(self, client: TestClient) -> None:
        """Summary respects date_from/date_to filters."""
        account = _create_account(client)
        self._seed(client, account["id"])
        resp = client.get(
            "/api/v1/summary",
            params={"date_from": "2024-01-01", "date_to": "2024-01-31"},
        )
        data = resp.json()
        assert data["overall_count"] == 2


# ── Health check ───────────────────────────────────────────────────────────────


def test_health_check(client: TestClient) -> None:
    """The /health endpoint returns 200 with status='healthy'."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"
