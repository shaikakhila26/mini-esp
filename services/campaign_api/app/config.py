import os
import secrets

from fastapi import Header, HTTPException, status


def get_campaign_api_key() -> str:
    """Read the configured API key, failing closed if it is missing."""
    api_key = os.environ.get("CAMPAIGN_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Campaign API authentication is not configured",
        )

    return api_key


def require_api_key(
    supplied_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Reject requests without the correct API key."""
    expected_key = get_campaign_api_key()

    if supplied_key is None or not secrets.compare_digest(
        supplied_key,
        expected_key,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )