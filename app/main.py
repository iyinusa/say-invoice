from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agents.parser import ParserNotConfigured
from app.db import get_sessionmaker
from app.logging_config import configure_logging, get_logger
from app.paypal import PayPalNotConfigured, close_paypal, get_paypal
from app.routers import (
    chase,
    clock,
    health,
    invoices,
    money,
    parse,
    plans,
    policy,
    quotes,
    ui,
    webhooks,
)
from app.routers.ui import STATIC_DIR
from app.services.payments import reconcile_forever
from app.settings import get_settings


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.env)
    # Before any ADK / google-genai code runs: they read credentials from env.
    settings.apply_google_env()
    get_logger("startup").info(
        "api_starting",
        env=settings.env,
        paypal_env=settings.paypal_env,
        paypal_configured=settings.paypal_configured,
        webhook_configured=bool(settings.paypal_webhook_id),
    )
    reconciler: asyncio.Task[None] | None = None
    if settings.paypal_configured and settings.reconcile_interval_s > 0:
        reconciler = asyncio.create_task(
            reconcile_forever(get_sessionmaker(), get_paypal(), settings.reconcile_interval_s)
        )
    yield
    if reconciler is not None:
        reconciler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconciler
    await close_paypal()
    get_logger("shutdown").info("api_stopping")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.env)

    app = FastAPI(
        title="SayInvoice",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url=None if settings.is_production else "/api-docs",
        redoc_url=None,
    )

    @app.exception_handler(PayPalNotConfigured)
    async def _paypal_not_configured(_req: Request, exc: PayPalNotConfigured) -> JSONResponse:
        return JSONResponse({"detail": f"PayPal is not configured: {exc}"}, status_code=503)

    @app.exception_handler(ParserNotConfigured)
    async def _parser_not_configured(_req: Request, exc: ParserNotConfigured) -> JSONResponse:
        return JSONResponse({"detail": f"Voice parsing is not configured: {exc}"}, status_code=503)

    app.include_router(health.router)
    app.include_router(parse.router)
    app.include_router(invoices.router)
    app.include_router(invoices.events_router)
    app.include_router(policy.router)
    app.include_router(quotes.router)
    app.include_router(quotes.public)
    app.include_router(quotes.approvals)
    app.include_router(quotes.jobs)
    app.include_router(money.router)
    app.include_router(chase.router)
    app.include_router(plans.public)
    app.include_router(plans.owner)
    app.include_router(clock.router)
    app.include_router(webhooks.router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(ui.router)

    return app


app = create_app()
