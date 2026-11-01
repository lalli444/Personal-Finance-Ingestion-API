"""
models.py
---------
SQLAlchemy ORM models for the Personal Finance Ingestion API.

Models:
    - Account:     Represents a bank / financial account.
    - Transaction: Represents an individual financial transaction.
"""

import hashlib
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ── Helpers ───────────────────────────────────────────────────────────────────


def _new_uuid() -> str:
    """Generate a new UUID4 string (used as primary key default)."""
    return str(uuid.uuid4())


# ── Account ───────────────────────────────────────────────────────────────────


class Account(Base):
    """
    A financial account (e.g. checking, savings, credit card).

    Attributes:
        id:           UUID primary key.
        account_name: Human-readable label for the account.
        institution:  Name of the bank or financial institution.
        account_type: Account category (checking, savings, credit, investment).
        created_at:   Server-side timestamp of record creation.
        transactions: Back-reference to all transactions under this account.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid, index=True
    )
    account_name: Mapped[str] = mapped_column(String(150), nullable=False)
    institution: Mapped[str] = mapped_column(String(150), nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="checking",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="account", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('checking', 'savings', 'credit', 'investment', 'other')",
            name="ck_account_type",
        ),
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id!r} name={self.account_name!r}>"


# ── Transaction ───────────────────────────────────────────────────────────────


class Transaction(Base):
    """
    A single financial transaction belonging to an Account.

    Attributes:
        id:          UUID primary key.
        date:        Date the transaction occurred (not the posting date).
        amount:      Decimal amount; negative = debit, positive = credit.
        currency:    ISO 4217 currency code (e.g. USD, EUR, GBP).
        description: Raw transaction narrative from the bank statement.
        category:    Optional spending category label.
        merchant:    Cleaned / normalised merchant name.
        account_id:  FK to Account.
        tx_hash:     SHA-256 fingerprint for duplicate detection.
        created_at:  Server-side timestamp of record creation.
    """

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_uuid, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=4), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD"
    )
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    tx_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    account: Mapped["Account"] = relationship("Account", back_populates="transactions")

    __table_args__ = (
        UniqueConstraint("tx_hash", name="uq_transaction_hash"),
        Index("ix_transactions_date", "date"),
        Index("ix_transactions_account_id", "account_id"),
        Index("ix_transactions_category", "category"),
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id!r} date={self.date!r} "
            f"amount={self.amount!r} merchant={self.merchant!r}>"
        )

    @staticmethod
    def build_hash(
        date: date,
        amount: float,
        description: str,
        account_id: str,
        merchant: str | None = None,
    ) -> str:
        """
        Build a deterministic SHA-256 fingerprint for duplicate detection.

        The hash encodes: date, amount (rounded to 4 dp), description
        (lowercased, stripped), merchant (lowercased, stripped), and account_id.
        """
        raw = "|".join(
            [
                str(date),
                f"{float(amount):.4f}",
                description.strip().lower(),
                (merchant or "").strip().lower(),
                account_id,
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()
