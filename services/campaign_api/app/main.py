from app.routes.recipients import router as recipients_router
from app.routes.campaigns import router as campaigns_router
from fastapi import FastAPI, HTTPException

from app.database import check_database

app = FastAPI(
    title="Mini ESP Campaign API",
    description="API for managing email campaigns.",
    version="0.1.0",
)

app.include_router(recipients_router)
app.include_router(campaigns_router)

@app.get("/health")
def health():
    """Check whether the API process is running."""
    return {"status": "ok"}


@app.get("/ready")
def readiness():
    """Check whether the API can access PostgreSQL."""
    if not check_database():
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )

    return {
        "status": "ready",
        "database": "connected",
    }