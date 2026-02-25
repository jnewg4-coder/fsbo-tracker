"""
FSBO Listing Tracker — Standalone FastAPI Application

Run with: uvicorn fsbo_tracker.app:app --port 8100 --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fsbo_tracker.router import router
from fsbo_tracker.auth_router import router as auth_router
from fsbo_tracker.billing_router import router as billing_router
from deal_pipeline.router import router as deal_router

logger = logging.getLogger("fsbo_tracker.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run migrations for all modules on startup."""
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

    yield


app = FastAPI(
    title="FSBO Listing Tracker API",
    description="Standalone API for FSBO deal discovery and tracking",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow local dev and future production domain
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

# Mount auth endpoints under /api/v2
app.include_router(auth_router, prefix="/api/v2")

# Mount all FSBO endpoints under /api/v2
app.include_router(router, prefix="/api/v2")

# Mount billing endpoints under /api/v2
app.include_router(billing_router, prefix="/api/v2")

# Mount deal pipeline endpoints under /api/v2
app.include_router(deal_router, prefix="/api/v2")


@app.get("/")
async def root():
    return {"service": "fsbo-tracker", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
