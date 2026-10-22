from fastapi import APIRouter, Depends, HTTPException, status

from app.config import require_api_key
from app.repositories import create_recipient
from app.schemas import RecipientCreate, RecipientResponse

router = APIRouter(
    prefix="/recipients",
    tags=["Recipients"],
)


@router.post(
    "",
    response_model=RecipientResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def register_recipient(payload: RecipientCreate):
    """Register a recipient without overwriting an existing registration."""

    recipient = create_recipient(
        email=str(payload.email),
        opted_in=payload.opted_in,
    )

    if recipient is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A recipient with this email address already exists",
        )

    return recipient