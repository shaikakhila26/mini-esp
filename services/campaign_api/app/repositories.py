from app.database import get_connection
import secrets
from uuid import UUID
from datetime import datetime, timezone

from app.database import get_connection


def create_recipient(email: str, opted_in: bool) -> dict| None:
    """Create a recipient and return the stored record.

    Returns None when the email address is already registered.
    """
    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO recipients (email, opted_in)
            VALUES (%s, %s)
            ON CONFLICT (email) DO NOTHING
            RETURNING id, email, opted_in
            """,
            (email, opted_in),
        ).fetchone()

        if row is None:
            return None

        return {
            "id": row[0],
            "email": row[1],
            "opted_in": row[2],
        }
class InvalidCampaignRecipients(Exception):
    pass


class CampaignIdempotencyConflict(Exception):
    pass


def _get_campaign_by_key(connection, idempotency_key: str):
    return connection.execute(
        """
        SELECT id, name, subject, html_body, from_email,
               tracking_enabled, status, created_at
        FROM campaigns
        WHERE idempotency_key = %s
        """,
        (idempotency_key,),
    ).fetchone()


def _get_campaign_by_id(connection, campaign_id: UUID):
    return connection.execute(
        """
        SELECT id, name, subject, html_body, from_email,
               tracking_enabled, status, created_at
        FROM campaigns
        WHERE id = %s
        """,
        (campaign_id,),
    ).fetchone()


def _campaign_details(connection, row) -> dict:
    campaign_id = row[0]

    recipient_rows = connection.execute(
        """
        SELECT recipient_id
        FROM messages
        WHERE campaign_id = %s
        ORDER BY recipient_id
        """,
        (campaign_id,),
    ).fetchall()

    link_rows = connection.execute(
        """
        SELECT destination_url
        FROM campaign_links
        WHERE campaign_id = %s
        ORDER BY link_no
        """,
        (campaign_id,),
    ).fetchall()

    return {
        "id": row[0],
        "name": row[1],
        "subject": row[2],
        "html_body": row[3],
        "from_email": row[4],
        "tracking_enabled": row[5],
        "status": row[6],
        "created_at": row[7],
        "recipient_ids": [r[0] for r in recipient_rows],
        "links": [r[0] for r in link_rows],
    }


def _matches_existing_campaign(
    connection,
    row,
    payload,
) -> bool:
    existing = _campaign_details(connection, row)

    return (
        existing["name"] == payload.name
        and existing["subject"] == payload.subject
        and existing["html_body"] == payload.html_body
        and existing["from_email"] == str(payload.from_email)
        and existing["tracking_enabled"] == payload.tracking_enabled
        and set(existing["recipient_ids"]) == set(payload.recipient_ids)
        and existing["links"] == payload.links
    )


def create_campaign(
    payload,
    idempotency_key: str,
) -> tuple[dict, bool]:
    with get_connection() as connection:
        # First, check whether this request has already been processed.
        existing = _get_campaign_by_key(
            connection,
            idempotency_key,
        )

        if existing is not None:
            if not _matches_existing_campaign(
                connection, existing, payload
            ):
                raise CampaignIdempotencyConflict()

            return _campaign_details(connection, existing), False

        # Only registered and opted-in recipients are eligible.
        recipient_rows = connection.execute(
            """
            SELECT id
            FROM recipients
            WHERE id = ANY(%s::uuid[])
              AND opted_in = TRUE
            """,
            (payload.recipient_ids,),
        ).fetchall()

        eligible_ids = {r[0] for r in recipient_rows}

        if eligible_ids != set(payload.recipient_ids):
            raise InvalidCampaignRecipients()

        # Create the campaign. The unique idempotency constraint also
        # protects against concurrent requests using the same key.
        row = connection.execute(
            """
            INSERT INTO campaigns (
                name,
                subject,
                html_body,
                from_email,
                tracking_enabled,
                status,
                idempotency_key
            )
            VALUES (%s, %s, %s, %s, %s, 'draft', %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, name, subject, html_body, from_email,
                      tracking_enabled, status, created_at
            """,
            (
                payload.name,
                payload.subject,
                payload.html_body,
                str(payload.from_email),
                payload.tracking_enabled,
                idempotency_key,
            ),
        ).fetchone()

        # Another request might have created the same key concurrently.
        if row is None:
            existing = _get_campaign_by_key(
                connection,
                idempotency_key,
            )

            if existing is None:
                raise RuntimeError(
                    "Campaign idempotency conflict could not be resolved"
                )

            if not _matches_existing_campaign(
                connection, existing, payload
            ):
                raise CampaignIdempotencyConflict()

            return _campaign_details(connection, existing), False

        campaign_id = row[0]

        # Store approved destinations, numbered from 1.
        for link_no, destination_url in enumerate(
            payload.links, start=1
        ):
            connection.execute(
                """
                INSERT INTO campaign_links (
                    campaign_id,
                    link_no,
                    destination_url
                )
                VALUES (%s, %s, %s)
                """,
                (campaign_id, link_no, destination_url),
            )

        # MVP decision: create the message records now, but do not send.
        # Tracking tokens are random and are not derived from email addresses.
        for recipient_id in payload.recipient_ids:
            connection.execute(
                """
                INSERT INTO messages (
                    campaign_id,
                    recipient_id,
                    token
                )
                VALUES (%s, %s, %s)
                """,
                (
                    campaign_id,
                    recipient_id,
                    secrets.token_urlsafe(16),
                ),
            )

        return _campaign_details(connection, row), True


def get_campaign(campaign_id: UUID) -> dict | None:
    with get_connection() as connection:
        row = _get_campaign_by_id(connection, campaign_id)

        if row is None:
            return None

        return _campaign_details(connection, row)

class CampaignNotFound(Exception):
    pass


class CampaignAlreadyStarted(Exception):
    pass


class TrackingMustBeDisabled(Exception):
    pass


class RecipientsNotEligible(Exception):
    pass


def claim_campaign_for_sending(campaign_id: UUID) -> tuple[dict, list[dict]]:
    """Atomically claim a draft campaign and validate its recipients."""

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, name, subject, html_body, from_email,
                   tracking_enabled, status
            FROM campaigns
            WHERE id = %s
            """,
            (campaign_id,),
        ).fetchone()

        if row is None:
            raise CampaignNotFound()

        if row[5]:
            raise TrackingMustBeDisabled()

        claimed = connection.execute(
            """
            UPDATE campaigns
            SET status = 'sending'
            WHERE id = %s
              AND status = 'draft'
            RETURNING id, name, subject, html_body, from_email,
                      tracking_enabled, status
            """,
            (campaign_id,),
        ).fetchone()

        if claimed is None:
            raise CampaignAlreadyStarted()

        message_rows = connection.execute(
            """
            SELECT m.id, m.recipient_id, r.email, r.opted_in
            FROM messages AS m
            JOIN recipients AS r
              ON r.id = m.recipient_id
            WHERE m.campaign_id = %s
            ORDER BY m.created_at, m.id
            """,
            (campaign_id,),
        ).fetchall()

        if not message_rows or any(not message[3] for message in message_rows):
            raise RecipientsNotEligible()

        campaign = {
            "id": claimed[0],
            "name": claimed[1],
            "subject": claimed[2],
            "html_body": claimed[3],
            "from_email": claimed[4],
            "tracking_enabled": claimed[5],
            "status": claimed[6],
        }

        messages = [
            {
                "id": message[0],
                "recipient_id": message[1],
                "email": message[2],
            }
            for message in message_rows
        ]

        return campaign, messages


def record_submission_event(
    message_id: UUID,
    event_type: str,
    idempotency_key: str,
    smtp_code: int | None = None,
    smtp_response: str | None = None,
) -> None:
    """Record a message-submission result without modifying existing events."""

    if event_type not in {"submitted", "submit_error"}:
        raise ValueError("Unsupported submission event type")

    occurred_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO events (
                message_id,
                event_type,
                occurred_at,
                source,
                idempotency_key,
                smtp_code,
                smtp_response
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                message_id,
                event_type,
                occurred_at,
                "campaign_api",
                idempotency_key,
                smtp_code,
                smtp_response[:1000] if smtp_response else None,
            ),
        )


def finish_campaign_send(campaign_id: UUID, final_status: str) -> None:
    """Set the final status after processing every message."""

    if final_status not in {"sent", "failed"}:
        raise ValueError("Final status must be 'sent' or 'failed'")

    with get_connection() as connection:
        result = connection.execute(
            """
            UPDATE campaigns
            SET status = %s
            WHERE id = %s
              AND status = 'sending'
            """,
            (final_status, campaign_id),
        )

        if result.rowcount != 1:
            raise CampaignAlreadyStarted(
                "Campaign status changed before sending could finish"
            )