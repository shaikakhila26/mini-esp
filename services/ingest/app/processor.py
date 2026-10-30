import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from psycopg.rows import dict_row

logger = logging.getLogger("ingest.processor")

EVENT_TYPE_MAP = {
    "Reception": "mta_accepted",
    "Delivery": "remote_accepted",
    "TransientFailure": "deferred",
    "Bounce": "failed",
    "Expiration": "failed",
}

class RecordError(ValueError):
    pass

def get_path(record, path):
    """Only use an explicitly configured dotted path; never guess fallbacks."""
    if not path:
        return None
    value = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value

def parse_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise RecordError("created_time is missing or invalid")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError("created_time is not valid ISO-8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def response_fields(record):
    response = record.get("response")
    if not isinstance(response, dict):
        return None, None
    code = response.get("code")
    if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
        code = None
    content = response.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)
    return code, content[:2000] if content is not None else None

def idempotency_key(record):
    """Stable for replay, while distinguishing Reception from Delivery."""
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    identity = {
        "id": record.get("id"),
        "type": record.get("type"),
        "created_time": record.get("created_time"),
        "num_attempts": record.get("num_attempts"),
        "response_code": response.get("code"),
        "response_content": response.get("content"),
        "recipient": record.get("recipient"),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def parse_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None

def process_record(conn, record, *, message_id_path, campaign_id_path):
    """Process one record inside the caller's transaction."""
    if not isinstance(record, dict):
        logger.warning("Ignoring non-object KumoMTA record")
        return "ignored"

    kind = record.get("type")
    if kind not in EVENT_TYPE_MAP:
        logger.info("Ignoring unsupported KumoMTA event type: %r", kind)
        return "ignored"

    mta_id = record.get("id")
    if not isinstance(mta_id, str) or not mta_id.strip():
        logger.warning("Skipping %s record without KumoMTA id", kind)
        return "skipped"

    message_id = parse_uuid(get_path(record, message_id_path))
    campaign_id = parse_uuid(get_path(record, campaign_id_path))
    if message_id is None or campaign_id is None:
        logger.warning(
            "Acknowledging uncorrelatable record type=%s id=%s; "
            "verified correlation paths are not configured or values are missing",
            kind, mta_id,
        )
        return "skipped"

    occurred_at = parse_timestamp(record.get("created_time"))
    smtp_code, smtp_response = response_fields(record)
    key = idempotency_key(record)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT id FROM messages WHERE id = %s AND campaign_id = %s""",
            (message_id, campaign_id),
        )
        if cur.fetchone() is None:
            logger.warning(
                "Acknowledging record type=%s id=%s: message/campaign pair not found",
                kind, mta_id,
            )
            return "skipped"

        cur.execute(
            """INSERT INTO events
                 (message_id, event_type, occurred_at, source, idempotency_key,
                  smtp_code, smtp_response)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id""",
            (message_id, EVENT_TYPE_MAP[kind], occurred_at, "kumomta",
             key, smtp_code, smtp_response),
        )
        return "inserted" if cur.fetchone() else "duplicate"

def process_payload(conn, payload, *, message_id_path, campaign_id_path, max_batch_records=500):
    records = payload if isinstance(payload, list) else [payload]
    if len(records) > max_batch_records:
        raise RecordError(f"Batch exceeds limit of {max_batch_records} records")
    counts = {"received": len(records), "inserted": 0, "duplicate": 0, "skipped": 0, "ignored": 0}
    # All records in a batch commit together; exceptions roll the transaction back.
    with conn.transaction():
        for record in records:
            result = process_record(
                conn, record,
                message_id_path=message_id_path,
                campaign_id_path=campaign_id_path,
            )
            counts[result] += 1
    return counts