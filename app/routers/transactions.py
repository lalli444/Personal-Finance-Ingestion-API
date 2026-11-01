"""
routers/transactions.py
-----------------------
Transaction query and analytics endpoints.

Endpoints:
    GET /api/v1/transactions  – Paginated, filtered transaction list.
    GET /api/v1/summary       – Aggregated spending summary.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.schemas import (
    PaginatedTransactions,
    SummaryResponse,
    TransactionFilter,
)

router = APIRouter(prefix="/transactions", tags=["Transactions"])
summary_router = APIRouter(prefix="/summary", tags=["Analytics"])


@router.get(
    "",
    response_model=PaginatedTransactions,
    summary="List transactions with optional filters and pagination",
)
def list_transactions(
    account_id: str | None = Query(default=None, description="Filter by account UUID"),
    category: str | None = Query(default=None, description="Filter by category"),
    date_from: date | None = Query(default=None, description="Inclusive start date (YYYY-MM-DD)"),
    date_to: date | None = Query(default=None, description="Inclusive end date (YYYY-MM-DD)"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),
    db: Session = Depends(get_db),
) -> PaginatedTransactions:
    """
    Return a paginated list of transactions.

    Supports optional filtering by:
    - ``account_id``: Restrict to a specific account.
    - ``category``: Exact-match category filter.
    - ``date_from`` / ``date_to``: Inclusive date range (ISO 8601).
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must not be later than date_to",
        )

    filters = TransactionFilter(
        account_id=account_id,
        category=category,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return crud.list_transactions(db, filters)


@summary_router.get(
    "",
    response_model=SummaryResponse,
    summary="Aggregated spending summary by category and month",
)
def get_summary(
    account_id: str | None = Query(default=None, description="Scope to a specific account UUID"),
    date_from: date | None = Query(default=None, description="Inclusive start date (YYYY-MM-DD)"),
    date_to: date | None = Query(default=None, description="Inclusive end date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """
    Return aggregated spending data grouped by:
    - **Category** – total spend per spending category.
    - **Month** – month-by-month totals with per-category breakdown.

    All filters are optional. Transactions without a category are grouped under
    *Uncategorised*.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must not be later than date_to",
        )

    data = crud.get_summary(
        db,
        account_id=account_id,
        date_from=date_from,
        date_to=date_to,
    )
    return SummaryResponse(**data)
