import os
from uuid import UUID
import smtplib

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Response,
    status,
)

from app.config import require_api_key
from app.repositories import (
    CampaignAlreadyStarted,
    CampaignIdempotencyConflict,
    CampaignNotFound,
    InvalidCampaignRecipients,
    RecipientsNotEligible,
    TrackingMustBeDisabled,
    claim_campaign_for_sending,
    create_campaign,
    finish_campaign_send,
    get_campaign,
    record_submission_event,
)
from app.schemas import (
    CampaignCreate,
    CampaignResponse,
    CampaignSendResponse,
)
from app.sending import build_email, send_email, validate_tracking_base_url

router = APIRouter(
    prefix="/campaigns",
    tags=["Campaigns"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign_endpoint(
    payload: CampaignCreate,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
):
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Idempotency-Key header is required",
        )

    if len(idempotency_key) > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must not exceed 200 characters",
        )

    try:
        campaign, created = create_campaign(
            payload,
            idempotency_key.strip(),
        )
    except InvalidCampaignRecipients:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One or more recipients are missing or not opted in",
        )
    except CampaignIdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This Idempotency-Key has already been used "
                "with different campaign data"
            ),
        )

    if not created:
        response.status_code = status.HTTP_200_OK

    return campaign


@router.get(
    "/{campaign_id}",
    response_model=CampaignResponse,
)
def get_campaign_endpoint(campaign_id: UUID):
    campaign = get_campaign(campaign_id)

    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    return campaign
@router.post(
    "/{campaign_id}/send",
    response_model=CampaignSendResponse,
)
def send_campaign_endpoint(campaign_id: UUID):
    # Validate tracking configuration before changing campaign status.
    tracking_base_url = os.environ.get("TRACK_BASE_URL")

    if tracking_base_url:
        try:
            tracking_base_url = validate_tracking_base_url(tracking_base_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )

    # Claim the campaign and validate its recipients before sending.
    try:
        campaign, messages = claim_campaign_for_sending(
            campaign_id,
            tracking_configured=bool(tracking_base_url),
        )

    except CampaignNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found",
        )

    except TrackingMustBeDisabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Tracking-enabled campaigns cannot be sent until "
                "the tracking hostname and endpoints are configured"
            ),
        )

    except CampaignAlreadyStarted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign has already started or is no longer a draft",
        )

    except RecipientsNotEligible:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Campaign has no messages or contains recipients "
                "who are not opted in"
            ),
        )

    submitted = 0
    failed = 0

    for item in messages:
        try:
            email_message = build_email(
                campaign_id=campaign["id"],
                message_id=item["id"],
                recipient_email=item["email"],
                from_email=campaign["from_email"],
                subject=campaign["subject"],
                html_body=campaign["html_body"],
                token=item["token"],
                tracking_enabled=campaign["tracking_enabled"],
                approved_links=campaign["links"],
                tracking_base_url=tracking_base_url,
            )

            send_email(email_message)

        except (
            smtplib.SMTPException,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            smtp_code = getattr(exc, "smtp_code", None)
            smtp_error = getattr(exc, "smtp_error", None)

            if isinstance(smtp_error, bytes):
                smtp_response = smtp_error.decode(
                    "utf-8",
                    errors="replace",
                )
            elif smtp_error is not None:
                smtp_response = str(smtp_error)
            else:
                smtp_response = str(exc)

            record_submission_event(
                message_id=item["id"],
                event_type="submit_error",
                idempotency_key=f"campaign_api:submit_error:{item['id']}",
                smtp_code=smtp_code,
                smtp_response=smtp_response,
            )

            failed += 1

        else:
            record_submission_event(
                message_id=item["id"],
                event_type="submitted",
                idempotency_key=f"campaign_api:submitted:{item['id']}",
            )

            submitted += 1

    final_status = "failed" if failed else "sent"

    finish_campaign_send(
        campaign_id=campaign_id,
        final_status=final_status,
    )

    return CampaignSendResponse(
        campaign_id=campaign_id,
        status=final_status,
        submitted=submitted,
        failed=failed,
        message=(
            "Campaign submission processing completed. "
            "Check the event records for individual results."
        ),
    )