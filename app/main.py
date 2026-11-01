"""
main.py
-------
FastAPI application factory for the Personal Finance Ingestion API.

Responsibilities:
    - Instantiate and configure the FastAPI application.
    - Mount all API routers under the versioned prefix.
    - Register global exception handlers for clean error responses.
    - Initialise the database on startup (SQLite dev mode).
"""

from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import get_settings
from app.database import init_db
from app.routers.ingest import accounts_router, router as ingest_router
from app.routers.transactions import router as transactions_router
from app.routers.transactions import summary_router

settings = get_settings()
logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan handler.

    On startup: initialise database tables (no-op if they already exist).
    On shutdown: nothing special is needed for synchronous SQLAlchemy.
    """
    logger.info("Starting up %s v%s [%s]", settings.app_name, settings.app_version, settings.app_env)
    if settings.app_env != "testing":
        try:
            init_db()
        except Exception as exc:
            logger.warning("Could not auto-initialise DB on startup: %s", exc)
    yield
    logger.info("Shutting down %s", settings.app_name)


# ── App factory ───────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "A production-grade REST API for ingesting, storing, and analysing "
            "personal finance transactions from JSON payloads and CSV bank statements."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    prefix = settings.api_prefix
    app.include_router(accounts_router, prefix=prefix)
    app.include_router(ingest_router, prefix=prefix)
    app.include_router(transactions_router, prefix=prefix)
    app.include_router(summary_router, prefix=prefix)

    # ── Exception handlers ────────────────────────────────────────────────────

    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        """Return a structured 422 response for Pydantic validation errors."""
        logger.warning("Pydantic validation error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "Validation Error",
                "detail": exc.errors(include_url=False),
            },
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        """Return a 409 Conflict when a database uniqueness constraint is violated."""
        logger.warning("Database integrity error: %s", exc.orig)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "Conflict",
                "detail": "A record with the same unique key already exists.",
            },
        )

    @app.exception_handler(OperationalError)
    async def db_operational_handler(
        request: Request, exc: OperationalError
    ) -> JSONResponse:
        """Return a 503 when the database is unreachable or misconfigured."""
        logger.error("Database operational error: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "Database Unavailable",
                "detail": "The database is temporarily unavailable. Please retry later.",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Catch-all handler that returns a 500 without leaking internals."""
        logger.error(
            "Unhandled exception on %s %s:\n%s",
            request.method,
            request.url,
            traceback.format_exc(),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal Server Error",
                "detail": "An unexpected error occurred. Please contact support.",
            },
        )

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health", tags=["Health"], summary="Health check endpoint")
    def health_check() -> dict:
        """Return service liveness status."""
        return {
            "status": "healthy",
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.app_env,
        }

    return app


app = create_app()
