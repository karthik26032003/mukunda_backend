import asyncio
import os
import logging
from fastapi import APIRouter, HTTPException
from helpers.ultravox import create_outbound_call
from models.outbound import (
    OutboundCallRequest,
    OutboundCallResponse,
    OutboundBatchRequest,
    OutboundBatchResponse,
    OutboundBatchResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbound", tags=["outbound"])


def _normalize_phone(number: str) -> str:
    """
    Normalize an Indian phone number to E.164 format (+91XXXXXXXXXX).

    Accepts:
      9876543210      → +919876543210
      919876543210    → +919876543210
      +919876543210   → +919876543210
      +91 98765 43210 → +919876543210
    """
    # Strip all spaces, dashes, parentheses
    cleaned = number.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if cleaned.startswith("+91"):
        digits = cleaned[3:]
    elif cleaned.startswith("91") and len(cleaned) == 12:
        digits = cleaned[2:]
    else:
        digits = cleaned.lstrip("+")

    return f"+91{digits}"


def _get_config() -> tuple[str, str]:
    """Returns (agent_id, from_number) or raises HTTPException."""
    agent_id    = os.getenv("AGENT_ID", "").strip().strip("'\"")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()

    if not agent_id:
        raise HTTPException(
            status_code=500,
            detail="AGENT_ID is not configured. Restart the server.",
        )
    if not from_number:
        raise HTTPException(
            status_code=500,
            detail="TWILIO_FROM_NUMBER is not configured in .env.",
        )
    return agent_id, from_number


@router.post("/call", response_model=OutboundCallResponse)
async def initiate_outbound_call(body: OutboundCallRequest):
    """
    POST /outbound/call
    Single outbound call to one phone number.
    """
    agent_id, from_number = _get_config()

    to_number = _normalize_phone(body.phone_number)
    logger.info(f"Outbound call → {to_number} (raw: {body.phone_number}) | agent: {agent_id}")

    try:
        call = await create_outbound_call(
            agent_id=agent_id,
            to_number=to_number,
            from_number=from_number,
        )
    except Exception as e:
        logger.error(f"Outbound call failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Call initiation failed: {str(e)}",
        )

    logger.info(f"Outbound call initiated: callId={call['callId']} → {body.phone_number}")

    return OutboundCallResponse(
        callId=call["callId"],
        status="initiated",
        to_number=to_number,
        message=f"Calling {to_number}. The AI will connect shortly.",
    )


@router.post("/calls/batch", response_model=OutboundBatchResponse)
async def initiate_batch_outbound_calls(body: OutboundBatchRequest):
    """
    POST /outbound/calls/batch
    Fire multiple outbound calls concurrently using asyncio.gather.
    Each number gets its own independent Ultravox call session.
    Returns a per-number success/failure summary.
    """
    agent_id, from_number = _get_config()

    logger.info(
        f"Batch outbound: {len(body.phone_numbers)} numbers | agent: {agent_id}"
    )

    async def _call_one(number: str) -> OutboundBatchResult:
        normalized = _normalize_phone(number)
        try:
            call = await create_outbound_call(
                agent_id=agent_id,
                to_number=normalized,
                from_number=from_number,
            )
            logger.info(f"Batch call OK: callId={call['callId']} → {normalized}")
            return OutboundBatchResult(
                phone_number=normalized,
                success=True,
                callId=call["callId"],
            )
        except Exception as e:
            logger.error(f"Batch call FAILED → {normalized}: {e}")
            return OutboundBatchResult(
                phone_number=normalized,
                success=False,
                error=str(e),
            )

    results = await asyncio.gather(*[_call_one(num) for num in body.phone_numbers])

    succeeded = sum(1 for r in results if r.success)

    return OutboundBatchResponse(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        results=list(results),
    )
