import re
from pydantic import BaseModel, field_validator


def _validate_e164(v: str) -> str:
    v = v.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+\d{7,15}$", v):
        raise ValueError(
            "Phone number must be in E.164 format: +[country_code][number]  "
            "e.g. +919876543210"
        )
    return v


class OutboundCallRequest(BaseModel):
    # Phone number in E.164 format: +[country_code][number]  e.g. +919876543210
    phone_number: str
    jd_text: str = ""

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return _validate_e164(v)


class OutboundCallResponse(BaseModel):
    callId: str
    status: str       # "initiated"
    to_number: str
    message: str


class OutboundBatchRequest(BaseModel):
    phone_numbers: list[str]

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("phone_numbers list cannot be empty")
        return [_validate_e164(num) for num in v]


class OutboundBatchResult(BaseModel):
    phone_number: str
    success: bool
    callId: str | None = None
    error: str | None = None


class OutboundBatchResponse(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: list[OutboundBatchResult]
