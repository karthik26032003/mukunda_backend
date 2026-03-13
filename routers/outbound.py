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

    logger.info(f"Outbound call → {body.phone_number} | agent: {agent_id}")

    try:
        call = await create_outbound_call(
            agent_id=agent_id,
            to_number=body.phone_number,
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
        to_number=body.phone_number,
        message=f"Calling {body.phone_number}. The AI will connect shortly.",
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
        try:
            call = await create_outbound_call(
                agent_id=agent_id,
                to_number=number,
                from_number=from_number,
            )
            logger.info(f"Batch call OK: callId={call['callId']} → {number}")
            return OutboundBatchResult(
                phone_number=number,
                success=True,
                callId=call["callId"],
            )
        except Exception as e:
            logger.error(f"Batch call FAILED → {number}: {e}")
            return OutboundBatchResult(
                phone_number=number,
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
