"""
routers/ingest.py
-----------------
Bulk ingestion endpoints for the Finance API.

Endpoints:
    POST /api/v1/ingest/json  – Accept a JSON payload of transactions.
    POST /api/v1/ingest/csv   – Accept a multipart CSV bank statement upload.
    POST /api/v1/accounts     – Create a new financial account.
    GET  /api/v1/accounts     – List all financial accounts.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud
from app.config import get_settings
from app.database import get_db
from app.models import Transaction
from app.schemas import (
    AccountCreate,
    AccountRead,
    BulkIngestRequest,
    IngestReport,
    RowError,
    TransactionCreate,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/ingest", tags=["Ingestion"])
accounts_router = APIRouter(prefix="/accounts", tags=["Accounts"])


# ── CSV column aliases ────────────────────────────────────────────────────────

# Map common CSV column names (lowercase) to our canonical field names.
_COLUMN_ALIASES: dict[str, str] = {
    # date variants
    "transaction date": "date",
    "trans date": "date",
    "txn date": "date",
    "posting date": "date",
    "value date": "date",
    # amount variants
    "debit": "amount",
    "credit": "amount",
    "transaction amount": "amount",
    "txn amount": "amount",
    # description variants
    "narrative": "description",
    "details": "description",
    "memo": "description",
    "particulars": "description",
    "reference": "description",
    # merchant variants
    "payee": "merchant",
    "vendor": "merchant",
    # category variants
    "type": "category",
    "tag": "category",
}

_REQUIRED_COLUMNS = {"date", "amount", "description"}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _normalise_df_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Lowercase and strip all column headers, then apply canonical aliases.
    Drops any completely-null columns.
    """
    df.columns = [str(c).strip().lower() for c in df.columns]
    df.rename(columns=_COLUMN_ALIASES, inplace=True)
    df.dropna(axis=1, how="all", inplace=True)
    return df


def _parse_csv_bytes(raw: bytes) -> pd.DataFrame:
    """
    Parse raw CSV bytes into a DataFrame with normalised columns.

    Raises:
        HTTPException 400: If the file cannot be parsed or required columns are missing.
    """
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, skip_blank_lines=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not parse CSV: {exc}",
        ) from exc

    df = _normalise_df_columns(df)
    missing = _REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV is missing required columns: {sorted(missing)}. "
                   f"Found columns: {sorted(df.columns.tolist())}",
        )

    if len(df) > settings.max_csv_rows:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"CSV exceeds the maximum of {settings.max_csv_rows} rows.",
        )

    return df


def _df_to_transaction_creates(
    df: pd.DataFrame,
    account_id: str,
) -> tuple[list[TransactionCreate], list[RowError]]:
    """
    Validate and convert each DataFrame row into a TransactionCreate.

    Returns:
        A tuple of (valid_items, failed_row_errors).
    """
    valid: list[TransactionCreate] = []
    errors: list[RowError] = []

    for idx, row in df.iterrows():
        raw: dict[str, Any] = {k: v for k, v in row.items() if pd.notna(v)}
        raw["account_id"] = account_id
        try:
            # Parse and normalise date
            raw_date = str(raw.get("date", "")).strip()
            parsed_date = pd.to_datetime(raw_date, dayfirst=False, errors="raise").date()

            # Parse amount
            raw_amount = str(raw.get("amount", "")).replace(",", "").strip()
            parsed_amount = float(raw_amount)

            tx = TransactionCreate(
                date=parsed_date,
                amount=parsed_amount,
                currency=str(raw.get("currency", "USD")).strip().upper() or "USD",
                description=str(raw.get("description", "")).strip(),
                category=str(raw.get("category", "")).strip() or None,
                merchant=str(raw.get("merchant", "")).strip() or None,
                account_id=account_id,
            )
            valid.append(tx)
        except Exception as exc:
            errors.append(
                RowError(row_index=int(idx), raw_data=raw, error=str(exc))  # type: ignore[arg-type]
            )

    return valid, errors


def _build_and_insert_transactions(
    db: Session,
    validated: list[TransactionCreate],
) -> tuple[int, int, list[RowError]]:
    """
    Deduplicate and bulk-insert validated transactions.

    Returns:
        (inserted_count, duplicate_count, failed_rows)
    """
    # Pre-compute all hashes
    hashed: list[tuple[TransactionCreate, str]] = []
    for tx in validated:
        h = Transaction.build_hash(
            date=tx.date,
            amount=tx.amount,
            description=tx.description,
            account_id=tx.account_id,
            merchant=tx.merchant,
        )
        hashed.append((tx, h))

    # Batch lookup existing hashes
    all_hashes = [h for _, h in hashed]
    existing = crud.get_existing_hashes(db, all_hashes)

    new_txs: list[Transaction] = []
    failed: list[RowError] = []
    duplicates = 0
    seen_in_batch: set[str] = set()

    for i, (tx, h) in enumerate(hashed):
        if h in existing:
            duplicates += 1
            continue
        if h in seen_in_batch:
            # duplicate within the same payload
            duplicates += 1
            continue
        seen_in_batch.add(h)
        new_txs.append(
            Transaction(
                date=tx.date,
                amount=tx.amount,
                currency=tx.currency,
                description=tx.description,
                category=tx.category,
                merchant=tx.merchant,
                account_id=tx.account_id,
                tx_hash=h,
            )
        )

    if new_txs:
        try:
            crud.bulk_create_transactions(db, new_txs)
        except IntegrityError as exc:
            db.rollback()
            logger.error("Bulk insert failed: %s", exc)
            failed.append(
                RowError(row_index=-1, raw_data={}, error=f"Database error: {exc.orig}")
            )
            return 0, duplicates, failed

    return len(new_txs), duplicates, failed


# ── Account routes ────────────────────────────────────────────────────────────


@accounts_router.post(
    "",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new financial account",
)
def create_account(
    payload: AccountCreate,
    db: Session = Depends(get_db),
) -> AccountRead:
    """Create and return a new Account record."""
    account = crud.create_account(db, payload)
    return AccountRead.model_validate(account)


@accounts_router.get(
    "",
    response_model=list[AccountRead],
    summary="List all financial accounts",
)
def list_accounts(db: Session = Depends(get_db)) -> list[AccountRead]:
    """Return all registered Account records."""
    accounts = crud.list_accounts(db)
    return [AccountRead.model_validate(a) for a in accounts]


# ── Ingestion routes ──────────────────────────────────────────────────────────


@router.post(
    "/json",
    response_model=IngestReport,
    status_code=status.HTTP_200_OK,
    summary="Bulk ingest transactions from JSON payload",
)
def ingest_json(
    payload: BulkIngestRequest,
    db: Session = Depends(get_db),
) -> IngestReport:
    """
    Accept a JSON body containing an array of transactions and persist them.

    Duplicate transactions (matched by SHA-256 hash) are silently skipped.
    Returns a detailed ingestion report with success / failure counts.
    """
    # Verify account exists
    account = crud.get_account(db, payload.account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{payload.account_id}' not found.",
        )

    inserted, duplicates, failed = _build_and_insert_transactions(
        db, payload.transactions
    )

    return IngestReport(
        total_received=len(payload.transactions),
        inserted=inserted,
        duplicates_skipped=duplicates,
        failed=len(failed),
        failed_rows=failed,
    )


@router.post(
    "/csv",
    response_model=IngestReport,
    status_code=status.HTTP_200_OK,
    summary="Bulk ingest transactions from a CSV file upload",
)
async def ingest_csv(
    account_id: str = Form(..., description="UUID of the target Account"),
    file: UploadFile = File(..., description="CSV bank statement file"),
    db: Session = Depends(get_db),
) -> IngestReport:
    """
    Accept a multipart CSV upload and ingest all valid transaction rows.

    The CSV must contain at minimum: ``date``, ``amount``, ``description``.
    Column names are matched case-insensitively and common aliases are supported
    (e.g. ``Transaction Date``, ``Payee``, ``Memo``).

    Returns a detailed ingestion report.
    """
    # Verify account exists
    account = crud.get_account(db, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{account_id}' not found.",
        )

    # Validate content type
    if file.content_type not in ("text/csv", "application/csv", "text/plain", "application/octet-stream"):
        logger.warning("Unexpected content-type '%s' for CSV upload", file.content_type)

    raw_bytes = await file.read()
    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.max_upload_size_mb} MB upload limit.",
        )

    df = _parse_csv_bytes(raw_bytes)
    valid, parse_errors = _df_to_transaction_creates(df, account_id)

    inserted, duplicates, db_errors = _build_and_insert_transactions(db, valid)

    all_errors = parse_errors + db_errors
    return IngestReport(
        total_received=len(df),
        inserted=inserted,
        duplicates_skipped=duplicates,
        failed=len(parse_errors) + len(db_errors),
        failed_rows=all_errors,
    )
