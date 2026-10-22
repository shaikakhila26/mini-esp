from datetime import datetime
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)


class RecipientCreate(BaseModel):
    email: EmailStr
    opted_in: bool = False


class RecipientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    opted_in: bool


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=500)
    html_body: str = Field(min_length=1)
    from_email: EmailStr
    tracking_enabled: bool = True

    recipient_ids: list[UUID] = Field(min_length=1)
    links: list[str] = Field(default_factory=list)

    @field_validator("recipient_ids")
    @classmethod
    def validate_unique_recipients(
        cls, recipient_ids: list[UUID]
    ) -> list[UUID]:
        if len(recipient_ids) != len(set(recipient_ids)):
            raise ValueError("Duplicate recipient IDs are not allowed")
        return recipient_ids

    @field_validator("links")
    @classmethod
    def validate_https_links(cls, links: list[str]) -> list[str]:
        for link in links:
            parsed = urlsplit(link)

            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "Every campaign link must be a valid HTTPS URL"
                )

        return links


class CampaignResponse(BaseModel):
    id: UUID
    name: str
    subject: str
    html_body: str
    from_email: EmailStr
    tracking_enabled: bool
    status: str
    created_at: datetime
    recipient_ids: list[UUID]
    links: list[str]

class CampaignSendResponse(BaseModel):
    campaign_id: UUID
    status: str
    submitted: int
    failed: int
    message: str