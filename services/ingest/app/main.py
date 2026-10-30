import hmac
import json
import logging

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.processor import RecordError, process_payload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ingest.api")
app = FastAPI(title="Mini ESP Event Ingestion API", docs_url=None, redoc_url=None, openapi_url=None)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/webhooks/kumomta")
async def kumomta_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
):
    settings = get_settings()
    if not x_webhook_secret or not hmac.compare_digest(x_webhook_secret, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_body_bytes:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")

    body = await request.body()
    if len(body) > settings.max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    try:
        with psycopg.connect(settings.database_url) as conn:
            counts = process_payload(
                conn, payload,
                message_id_path=settings.message_id_path,
                campaign_id_path=settings.campaign_id_path,
                max_batch_records=settings.max_batch_records,
            )
    except RecordError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except psycopg.Error:
        logger.exception("Database error while ingesting KumoMTA webhook")
        raise HTTPException(status_code=503, detail="Temporary persistence failure")

    return JSONResponse(status_code=200, content={"status": "accepted", **counts})