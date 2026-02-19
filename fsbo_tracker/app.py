"""
FSBO Listing Tracker — Standalone FastAPI Application

Run with: uvicorn fsbo_tracker.app:app --port 8100 --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fsbo_tracker.router import router

app = FastAPI(
    title="FSBO Listing Tracker API",
    description="Standalone API for FSBO deal discovery and tracking",
    version="1.0.0",
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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all FSBO endpoints under /api/v2
app.include_router(router, prefix="/api/v2")


@app.get("/")
async def root():
    return {"service": "fsbo-tracker", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
