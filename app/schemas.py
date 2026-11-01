"""
schemas.py
----------
Pydantic v2 schemas for request validation and response serialisation.

Schema groups:
    - Account:     Create / Read schemas for Account records.
    - Transaction: Create / Read / Filter schemas for Transaction records.
    - Ingestion:   Request and result schemas for the bulk ingestion endpoint.
    - Summary:     Response schemas for the analytics/summary endpoint.
"""

from __future__ import annotations

from datetime import date as dt_date, datetime as dt_datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


# ── Shared config ─────────────────────────────────────────────────────────────


class _OrmBase(BaseModel):
    """Base model with ORM mode enabled for SQLAlchemy compatibility."""

    model_config = ConfigDict(from_attributes=True)


# ── Account schemas ───────────────────────────────────────────────────────────


class AccountCreate(BaseModel):
    """Payload required to create a new Account."""

    account_name: str = Field(..., min_length=1, max_length=150, examples=["Chase Checking"])
    institution: str = Field(..., min_length=1, max_length=150, examples=["Chase Bank"])
    account_type: Literal["checking", "savings", "credit", "investment", "other"] = (
        Field(default="checking")
    )


class AccountRead(_OrmBase):
    """Serialised Account returned by the API."""

    id: str
    account_name: str
    institution: str
    account_type: str
    created_at: dt_datetime


# ── Transaction schemas ───────────────────────────────────────────────────────


class TransactionCreate(BaseModel):
    """
    Payload for a single transaction.
    Used both directly and as a row inside BulkIngestRequest.
    """

    date: dt_date = Field(..., description="Transaction date (YYYY-MM-DD)")
    amount: float = Field(..., description="Positive = credit, negative = debit")
    currency: Annotated[str, StringConstraints(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")] = "USD"
    description: str = Field(..., min_length=1, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    merchant: str | None = Field(default=None, max_length=200)
    account_id: str = Field(..., description="UUID of the owning Account")

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("description", "merchant", "category", mode="before")
    @classmethod
    def strip_strings(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class TransactionRead(_OrmBase):
    """Serialised Transaction returned by the API."""

    id: str
    date: dt_date
    amount: float
    currency: str
    description: str
    category: str | None
    merchant: str | None
    account_id: str
    tx_hash: str
    created_at: dt_datetime


class TransactionFilter(BaseModel):
    """Query parameters for listing / filtering transactions."""

    account_id: str | None = Field(default=None, description="Filter by account UUID")
    category: str | None = Field(default=None, description="Filter by category label")
    date_from: dt_date | None = Field(default=None, description="Inclusive start date")
    date_to: dt_date | None = Field(default=None, description="Inclusive end date")
    page: int = Field(default=1, ge=1, description="1-based page number")
    page_size: int = Field(default=50, ge=1, le=500, description="Results per page")

    @model_validator(mode="after")
    def validate_date_range(self) -> "TransactionFilter":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        return self


class PaginatedTransactions(BaseModel):
    """Paginated response wrapper for transaction listings."""

    total: int
    page: int
    page_size: int
    pages: int
    items: list[TransactionRead]


# ── Ingestion schemas ─────────────────────────────────────────────────────────


class BulkIngestRequest(BaseModel):
    """JSON body for bulk transaction ingestion."""

    account_id: str = Field(..., description="UUID of the target Account")
    transactions: list[TransactionCreate] = Field(
        ..., min_length=1, max_length=50_000
    )


class RowError(BaseModel):
    """Details about a single failed row during ingestion."""

    row_index: int = Field(..., description="0-based index of the failed row")
    raw_data: dict[str, Any] = Field(default_factory=dict)
    error: str


class IngestReport(BaseModel):
    """Summary report returned after a bulk ingestion attempt."""

    total_received: int
    inserted: int
    duplicates_skipped: int
    failed: int
    failed_rows: list[RowError] = Field(default_factory=list)


# ── Summary / Analytics schemas ───────────────────────────────────────────────


class CategorySummary(BaseModel):
    """Spending total for a single category."""

    category: str
    total_amount: float
    transaction_count: int


class MonthlySummary(BaseModel):
    """Spending totals grouped by year-month."""

    year: int
    month: int
    total_amount: float
    transaction_count: int
    breakdown: list[CategorySummary] = Field(default_factory=list)


class SummaryResponse(BaseModel):
    """Full analytics summary response."""

    account_id: str | None
    date_from: dt_date | None
    date_to: dt_date | None
    overall_total: float
    overall_count: int
    by_category: list[CategorySummary]
    by_month: list[MonthlySummary]
