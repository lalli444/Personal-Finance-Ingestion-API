"""
crud.py
-------
Database access layer for the Finance API.

All database mutations and reads are centralised here to keep routers thin
and make the persistence logic easy to unit-test in isolation.
"""

from __future__ import annotations

import math
from datetime import date

from sqlalchemy import extract, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Account, Transaction
from app.schemas import (
    AccountCreate,
    CategorySummary,
    MonthlySummary,
    PaginatedTransactions,
    TransactionCreate,
    TransactionFilter,
    TransactionRead,
)


# ── Account CRUD ──────────────────────────────────────────────────────────────


def create_account(db: Session, payload: AccountCreate) -> Account:
    """
    Persist a new Account record.

    Args:
        db:      Active SQLAlchemy session.
        payload: Validated AccountCreate schema.

    Returns:
        The newly created Account ORM instance.
    """
    account = Account(
        account_name=payload.account_name,
        institution=payload.institution,
        account_type=payload.account_type,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def get_account(db: Session, account_id: str) -> Account | None:
    """Fetch a single Account by primary key, or None if not found."""
    return db.get(Account, account_id)


def list_accounts(db: Session) -> list[Account]:
    """Return all Account records ordered by creation timestamp."""
    stmt = select(Account).order_by(Account.created_at)
    return list(db.scalars(stmt).all())


# ── Transaction CRUD ──────────────────────────────────────────────────────────


def create_transaction(
    db: Session,
    payload: TransactionCreate,
    tx_hash: str,
) -> Transaction:
    """
    Insert a single validated Transaction into the database.

    Args:
        db:      Active SQLAlchemy session.
        payload: Validated TransactionCreate schema.
        tx_hash: Pre-computed SHA-256 fingerprint for deduplication.

    Returns:
        The persisted Transaction ORM instance.

    Raises:
        IntegrityError: If a duplicate tx_hash already exists.
    """
    tx = Transaction(
        date=payload.date,
        amount=payload.amount,
        currency=payload.currency,
        description=payload.description,
        category=payload.category,
        merchant=payload.merchant,
        account_id=payload.account_id,
        tx_hash=tx_hash,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def bulk_create_transactions(
    db: Session,
    transactions: list[Transaction],
) -> list[Transaction]:
    """
    Flush a pre-built list of Transaction ORM objects in a single commit.

    Args:
        db:           Active SQLAlchemy session.
        transactions: List of un-persisted Transaction instances.

    Returns:
        The same list after flush (objects now have server-assigned fields).
    """
    db.add_all(transactions)
    db.commit()
    for tx in transactions:
        db.refresh(tx)
    return transactions


def hash_exists(db: Session, tx_hash: str) -> bool:
    """Return True when a transaction with the given hash already exists."""
    stmt = select(Transaction.id).where(Transaction.tx_hash == tx_hash).limit(1)
    return db.scalars(stmt).first() is not None


def get_existing_hashes(db: Session, hashes: list[str]) -> set[str]:
    """
    Return the subset of *hashes* that already exist in the database.
    Efficient batch lookup used during bulk ingestion.
    """
    stmt = select(Transaction.tx_hash).where(Transaction.tx_hash.in_(hashes))
    return set(db.scalars(stmt).all())


def list_transactions(
    db: Session,
    filters: TransactionFilter,
) -> PaginatedTransactions:
    """
    Return a paginated, filtered list of transactions.

    Args:
        db:      Active SQLAlchemy session.
        filters: TransactionFilter with optional account_id, category, dates.

    Returns:
        PaginatedTransactions with metadata and a list of TransactionRead items.
    """
    stmt = select(Transaction)

    if filters.account_id:
        stmt = stmt.where(Transaction.account_id == filters.account_id)
    if filters.category:
        stmt = stmt.where(Transaction.category == filters.category)
    if filters.date_from:
        stmt = stmt.where(Transaction.date >= filters.date_from)
    if filters.date_to:
        stmt = stmt.where(Transaction.date <= filters.date_to)

    # Total count
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total: int = db.scalar(count_stmt) or 0

    # Paginate
    offset = (filters.page - 1) * filters.page_size
    stmt = stmt.order_by(Transaction.date.desc()).offset(offset).limit(filters.page_size)
    items = list(db.scalars(stmt).all())

    return PaginatedTransactions(
        total=total,
        page=filters.page,
        page_size=filters.page_size,
        pages=math.ceil(total / filters.page_size) if total else 0,
        items=[TransactionRead.model_validate(tx) for tx in items],
    )


# ── Analytics / Summary ───────────────────────────────────────────────────────


def get_summary(
    db: Session,
    account_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """
    Compute spending summaries grouped by category and month.

    Args:
        db:         Active SQLAlchemy session.
        account_id: Optional account scope.
        date_from:  Inclusive start date.
        date_to:    Inclusive end date.

    Returns:
        A dict ready to be hydrated into a SummaryResponse schema.
    """
    conditions = []
    if account_id:
        conditions.append(Transaction.account_id == account_id)
    if date_from:
        conditions.append(Transaction.date >= date_from)
    if date_to:
        conditions.append(Transaction.date <= date_to)

    # --- Overall totals ---
    agg_stmt = select(
        func.sum(Transaction.amount).label("total_amount"),
        func.count(Transaction.id).label("total_count"),
    )
    if conditions:
        agg_stmt = agg_stmt.where(*conditions)
    agg_row = db.execute(agg_stmt).one()
    overall_total = float(agg_row.total_amount or 0)
    overall_count = int(agg_row.total_count or 0)

    # --- By category ---
    cat_stmt = select(
        func.coalesce(Transaction.category, "Uncategorised").label("category"),
        func.sum(Transaction.amount).label("total_amount"),
        func.count(Transaction.id).label("transaction_count"),
    )
    if conditions:
        cat_stmt = cat_stmt.where(*conditions)
    cat_stmt = (
        cat_stmt
        .group_by(func.coalesce(Transaction.category, "Uncategorised"))
        .order_by(func.sum(Transaction.amount))
    )
    cat_rows = db.execute(cat_stmt).all()
    by_category = [
        CategorySummary(
            category=row.category,
            total_amount=float(row.total_amount),
            transaction_count=int(row.transaction_count),
        )
        for row in cat_rows
    ]

    # --- By month with per-month category breakdown ---
    month_stmt = select(
        extract("year", Transaction.date).label("year"),
        extract("month", Transaction.date).label("month"),
        func.coalesce(Transaction.category, "Uncategorised").label("category"),
        func.sum(Transaction.amount).label("total_amount"),
        func.count(Transaction.id).label("transaction_count"),
    )
    if conditions:
        month_stmt = month_stmt.where(*conditions)
    month_stmt = (
        month_stmt
        .group_by(
            extract("year", Transaction.date),
            extract("month", Transaction.date),
            func.coalesce(Transaction.category, "Uncategorised"),
        )
        .order_by(
            extract("year", Transaction.date),
            extract("month", Transaction.date),
        )
    )
    month_rows = db.execute(month_stmt).all()

    # Aggregate month rows into MonthlySummary objects
    month_map: dict[tuple[int, int], MonthlySummary] = {}
    for row in month_rows:
        key = (int(row.year), int(row.month))
        if key not in month_map:
            month_map[key] = MonthlySummary(
                year=key[0],
                month=key[1],
                total_amount=0.0,
                transaction_count=0,
                breakdown=[],
            )
        month_map[key].total_amount += float(row.total_amount)
        month_map[key].transaction_count += int(row.transaction_count)
        month_map[key].breakdown.append(
            CategorySummary(
                category=row.category,
                total_amount=float(row.total_amount),
                transaction_count=int(row.transaction_count),
            )
        )

    return {
        "account_id": account_id,
        "date_from": date_from,
        "date_to": date_to,
        "overall_total": overall_total,
        "overall_count": overall_count,
        "by_category": by_category,
        "by_month": list(month_map.values()),
    }
