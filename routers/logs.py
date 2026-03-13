import os
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from helpers.ultravox import get_agent_calls, get_call_messages, get_call_recording
from models.logs import CallsListResponse, CallSummary, CallMessagesResponse, MessageItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])

# Roles we want to surface in the transcript (strip tool calls/results)
_VISIBLE_ROLES = {"MESSAGE_ROLE_USER", "MESSAGE_ROLE_AGENT"}
_ROLE_MAP = {
    "MESSAGE_ROLE_USER":  "user",
    "MESSAGE_ROLE_AGENT": "agent",
}
_MEDIUM_MAP = {
    "MESSAGE_MEDIUM_VOICE": "voice",
    "MESSAGE_MEDIUM_TEXT":  "text",
}


def _parse_duration(joined: str | None, ended: str | None, billed: str | None) -> str | None:
    """Return a human-readable duration string."""
    # Prefer billedDuration (e.g. "257.3s") if available
    if billed:
        try:
            secs = float(billed.rstrip("s"))
            m, s = divmod(int(secs), 60)
            return f"{m}m {s}s" if m else f"{s}s"
        except ValueError:
            pass

    # Fallback: calculate from joined / ended timestamps
    if joined and ended:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            def _parse(ts: str):
                ts = ts.replace("+00:00", "Z")
                for f in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
                    try:
                        return datetime.strptime(ts, f).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                return None
            t_start = _parse(joined)
            t_end   = _parse(ended)
            if t_start and t_end:
                secs = int((t_end - t_start).total_seconds())
                m, s = divmod(secs, 60)
                return f"{m}m {s}s" if m else f"{s}s"
        except Exception:
            pass

    return None


def _extract_medium(medium_obj: dict | None) -> str | None:
    """Return the telephony medium name from the medium object."""
    if not medium_obj:
        return None
    for key in ("webRtc", "plivo", "twilio", "telnyx", "exotel", "sip", "webSocket"):
        if key in medium_obj:
            return key
    return None


@router.get("/calls", response_model=CallsListResponse)
async def list_calls(
    page_size: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None),
):
    """
    GET /logs/calls
    Returns all calls for the configured agent, newest first.
    """
    agent_id = os.getenv("AGENT_ID", "").strip().strip("'\"")
    if not agent_id:
        raise HTTPException(status_code=500, detail="AGENT_ID is not configured.")

    try:
        data = await get_agent_calls(agent_id, cursor=cursor, page_size=page_size)
    except Exception as e:
        logger.error(f"Failed to fetch calls: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    results = []
    for call in data.get("results", []):
        results.append(CallSummary(
            callId       = call.get("callId", ""),
            created      = call.get("created"),
            joined       = call.get("joined"),
            ended        = call.get("ended"),
            duration     = _parse_duration(
                               call.get("joined"),
                               call.get("ended"),
                               call.get("billedDuration"),
                           ),
            endReason    = call.get("endReason"),
            shortSummary = call.get("shortSummary"),
            medium       = _extract_medium(call.get("medium")),
        ))

    # Extract cursor tokens from the next/previous URLs
    def _cursor_from_url(url: str | None) -> str | None:
        if not url:
            return None
        for part in url.split("&"):
            if part.startswith("cursor=") or "cursor=" in part:
                return part.split("cursor=")[-1]
        return None

    return CallsListResponse(
        total    = data.get("total", len(results)),
        next     = _cursor_from_url(data.get("next")),
        previous = _cursor_from_url(data.get("previous")),
        results  = results,
    )


@router.get("/calls/{call_id}/messages", response_model=CallMessagesResponse)
async def get_messages(call_id: str):
    """
    GET /logs/calls/{call_id}/messages
    Returns the cleaned transcript (agent + user only) for a single call.
    """
    try:
        data = await get_call_messages(call_id)
    except Exception as e:
        logger.error(f"Failed to fetch messages for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    messages = []
    for msg in data.get("results", []):
        role = msg.get("role", "")
        if role not in _VISIBLE_ROLES:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        messages.append(MessageItem(
            role   = _ROLE_MAP[role],
            text   = text,
            medium = _MEDIUM_MAP.get(msg.get("medium", ""), None),
        ))

    return CallMessagesResponse(
        callId   = call_id,
        total    = len(messages),
        messages = messages,
    )


@router.get("/calls/{call_id}/recording")
async def get_recording(call_id: str):
    """
    GET /logs/calls/{call_id}/recording
    Proxies the WAV recording from Ultravox back to the client.
    Ultravox returns either the audio directly (200) or via redirect (302),
    both handled transparently by follow_redirects=True in the helper.

    Content-Type: audio/wav
    Content-Disposition: attachment — browser will download it.
    Add ?stream=1 if you want inline playback instead.
    """
    try:
        resp = await get_call_recording(call_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            raise HTTPException(status_code=404, detail="Recording not available for this call.")
        logger.error(f"Failed to fetch recording for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")
    except Exception as e:
        logger.error(f"Failed to fetch recording for {call_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Ultravox API error: {str(e)}")

    content_type = resp.headers.get("content-type", "audio/wav")

    def _iter():
        yield resp.content

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={
            # inline → browser audio element can play it directly
            # The native <audio> download button handles saving if needed
            "Content-Disposition": f'inline; filename="recording_{call_id}.wav"',
            "Content-Length": str(len(resp.content)),
            "Accept-Ranges": "bytes",
        },
    )
