"""Mini ESP Tracking API.

Routes:
  GET  /o/{token}.gif       Open tracking pixel
  HEAD /o/{token}.gif       Pixel probe; never records an event
  GET  /c/{token}/{link_no} Click tracking and safe redirect
  HEAD /c/{token}/{link_no} Link probe; never records an event
  GET  /health              Liveness and database readiness
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TRACKING_SOURCE = os.getenv("TRACKING_SOURCE", "tracking_api").strip() or "tracking_api"

# Valid transparent 1x1 GIF.
PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff"
    "21f90401000000002c00000000010001000002024401003b"
)

SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
PIXEL_HEADERS = {
    **SECURITY_HEADERS,
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

app = FastAPI(
    title="Mini ESP Tracking API",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def get_connection() -> psycopg.Connection:
    """Open a short-lived connection. DATABASE_URL is required."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=3)


def find_message(token: str) -> dict[str, Any] | None:
    """Resolve token to message and campaign tracking settings."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT m.id AS message_id,
                   m.campaign_id,
                   c.tracking_enabled
            FROM messages AS m
            JOIN campaigns AS c ON c.id = m.campaign_id
            WHERE m.token = %s
            """,
            (token,),
        ).fetchone()


def find_destination(campaign_id: Any, link_no: int) -> str | None:
    """Return only the destination stored for this campaign/link pair."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT destination_url
            FROM campaign_links
            WHERE campaign_id = %s AND link_no = %s
            """,
            (campaign_id, link_no),
        ).fetchone()
    if row is None:
        return None

    destination = row["destination_url"]
    # Defense in depth: database constraints require https://, but validate
    # the parsed URL before using it as a Location header.
    parsed = urlsplit(destination)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or destination != destination.strip()
    ):
        return None
    return destination


def record_observation(
    *,
    message_id: Any,
    event_type: str,
    user_agent: str | None,
    link_no: int | None = None,
) -> None:
    """Append one observation with a fresh key; never update/delete events."""
    # Each request is an independent observation. A random key ensures that
    # repeated opens/clicks remain separate rows, as specified by the plan.
    event_key = secrets.token_urlsafe(32)
    occurred_at = datetime.now(timezone.utc)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO events (
                message_id,
                event_type,
                occurred_at,
                source,
                idempotency_key,
                link_no,
                user_agent
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                message_id,
                event_type,
                occurred_at,
                TRACKING_SOURCE,
                event_key,
                link_no,
                user_agent,
            ),
        )
        # psycopg's connection context commits on successful exit and rolls
        # back if the insert raises an exception.


def pixel_response() -> Response:
    return Response(
        content=PIXEL_GIF,
        media_type="image/gif",
        headers=PIXEL_HEADERS,
    )


def request_user_agent(request: Request) -> str | None:
    # Do not store IP addresses or recipient email addresses.
    return request.headers.get("user-agent")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness plus database readiness check."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "database": "ok"}


@app.get("/o/{token}.gif", include_in_schema=False)
def track_open(token: str, request: Request) -> Response:
    """Record an eligible GET observation and always return the pixel."""
    try:
        message = find_message(token)
        if message is not None and message["tracking_enabled"]:
            record_observation(
                message_id=message["message_id"],
                event_type="open",
                user_agent=request_user_agent(request),
            )
    except (psycopg.Error, RuntimeError):
        # Do not silently claim the observation was recorded. This also avoids
        # returning a normal pixel response during a database outage.
        raise HTTPException(status_code=503, detail="tracking temporarily unavailable")
    return pixel_response()


@app.head("/o/{token}.gif", include_in_schema=False)
def probe_open(token: str) -> Response:
    """Answer pixel probes without token lookup or event recording."""
    return pixel_response()


@app.get("/c/{token}/{link_no}", include_in_schema=False)
def track_click(token: str, link_no: int, request: Request) -> RedirectResponse:
    """Record an eligible click, then redirect only to a stored HTTPS URL."""
    if link_no <= 0:
        raise HTTPException(status_code=404, detail="link not found")

    try:
        message = find_message(token)
        if message is None:
            raise HTTPException(status_code=404, detail="link not found")

        destination = find_destination(message["campaign_id"], link_no)
        if destination is None:
            raise HTTPException(status_code=404, detail="link not found")

        if message["tracking_enabled"]:
            record_observation(
                message_id=message["message_id"],
                event_type="click",
                user_agent=request_user_agent(request),
                link_no=link_no,
            )
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError):
        # Do not redirect if the event couldn't be persisted when tracking is on.
        raise HTTPException(status_code=503, detail="tracking temporarily unavailable")

    return RedirectResponse(
        url=destination,
        status_code=302,
        headers=SECURITY_HEADERS,
    )


@app.head("/c/{token}/{link_no}", include_in_schema=False)
def probe_click(token: str, link_no: int) -> Response:
    """Validate and resolve link probes, but never record observations."""
    if link_no <= 0:
        raise HTTPException(status_code=404, detail="link not found")
    try:
        message = find_message(token)
        if message is None:
            raise HTTPException(status_code=404, detail="link not found")
        destination = find_destination(message["campaign_id"], link_no)
        if destination is None:
            raise HTTPException(status_code=404, detail="link not found")
    except HTTPException:
        raise
    except (psycopg.Error, RuntimeError):
        raise HTTPException(status_code=503, detail="tracking temporarily unavailable")

    return Response(
        status_code=302,
        headers={**SECURITY_HEADERS, "Location": destination},
    )
