"""
FSBO Listing Tracker — Standalone FastAPI Application

Run with: uvicorn fsbo_tracker.app:app --port 8100 --reload
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from fsbo_tracker.router import router
from fsbo_tracker.auth_router import router as auth_router
from fsbo_tracker.billing_router import router as billing_router
from fsbo_tracker.notification_router import router as notification_router
from fsbo_tracker.advisor_router import router as advisor_router
from fsbo_tracker.rate_limit import limiter
from deal_pipeline.router import router as deal_router

logger = logging.getLogger("fsbo_tracker.app")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()


def _run_pipeline_job():
    """Scheduled daily pipeline run — wrapped so exceptions never kill the scheduler."""
    try:
        from fsbo_tracker.tracker import run_daily
        logger.info("[Scheduler] Starting daily pipeline run")
        summary = run_daily()
        logger.info("[Scheduler] Pipeline complete: %s", summary)
    except Exception as e:
        logger.error("[Scheduler] Pipeline job failed: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run migrations on startup, start daily pipeline scheduler, verify Slack."""
    try:
        from fsbo_tracker.db import run_migration as fsbo_migrate
        fsbo_migrate()
        logger.info("fsbo_tracker migrations applied")
    except Exception as e:
        logger.error("fsbo_tracker migration failed: %s", e)

    try:
        from deal_pipeline.db import run_migration as deal_migrate
        deal_migrate()
        logger.info("deal_pipeline migrations applied")
    except Exception as e:
        logger.error("deal_pipeline migration failed: %s", e)

    # Verify Slack on deploy
    try:
        from fsbo_tracker.slack_alerts import get_alerter
        alerter = get_alerter()
        if alerter.enabled:
            result = alerter.send_test_alert()
            logger.info("Slack test alert: %s", result)
    except Exception as e:
        logger.warning("Slack test alert failed: %s", e)

    # Daily pipeline scheduler — 6 AM US/Eastern (auto-handles EST/EDT)
    scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            _run_pipeline_job,
            trigger=CronTrigger(hour=6, minute=0, timezone="US/Eastern"),
            id="daily_pipeline",
            replace_existing=True,
            misfire_grace_time=3600,  # fire even if up to 1hr late (Railway cold start)
        )
        scheduler.start()
        logger.info("[Scheduler] Daily pipeline scheduled at 06:00 US/Eastern")
    except Exception as e:
        logger.error("[Scheduler] Failed to start scheduler: %s", e)

    yield

    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="FSBO Listing Tracker API",
    description="Standalone API for FSBO deal discovery and tracking",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiter (global) — shared instance from rate_limit module
# ---------------------------------------------------------------------------
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """429 with retry info."""
    retry_after = 60
    if hasattr(exc, "detail") and "Retry after" in str(exc.detail):
        try:
            retry_after = int(str(exc.detail).split("Retry after ")[1].split(" ")[0])
        except (IndexError, ValueError):
            pass

    logger.warning("Rate limit exceeded: %s %s", request.method, request.url.path)

    return JSONResponse(
        status_code=429,
        content={
            "success": False,
            "error": "rate_limit_exceeded",
            "message": "Too many requests. Please wait before trying again.",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8888",
        "http://localhost:8100",
        "http://127.0.0.1:8888",
        "https://fsbo.avmlens.app",
        "https://fsbo-tracker.netlify.app",
        "https://fsbo-api-production.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request logging middleware (request ID + timing)
# ---------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.time()

    response = await call_next(request)

    duration_ms = int((time.time() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{duration_ms}ms"

    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(level, "%s %s %d %dms [%s]",
               request.method, request.url.path, response.status_code,
               duration_ms, request_id)

    return response


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")

    logger.error("Unhandled exception [%s] %s: %s",
                 request_id, type(exc).__name__, exc, exc_info=True)

    # Fire Slack alert
    try:
        from fsbo_tracker.slack_alerts import get_alerter
        get_alerter().alert_error(type(exc).__name__, str(exc), request_id)
    except Exception:
        pass

    is_dev = ENVIRONMENT in ("development", "dev", "local")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": str(exc) if is_dev else "An internal error occurred",
            "error_code": "INTERNAL_ERROR",
            "request_id": request_id,
            "message": "Please contact support with your request_id if this persists",
        },
    )


# ---------------------------------------------------------------------------
# Mount routers
# ---------------------------------------------------------------------------
app.include_router(auth_router, prefix="/api/v2")
app.include_router(router, prefix="/api/v2")
app.include_router(billing_router, prefix="/api/v2")
app.include_router(notification_router, prefix="/api/v2")
app.include_router(advisor_router, prefix="/api/v2")
app.include_router(deal_router, prefix="/api/v2")


@app.get("/")
async def root():
    return {"service": "fsbo-tracker", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
