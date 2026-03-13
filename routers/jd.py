import logging

from fastapi import APIRouter, File, HTTPException, UploadFile

from helpers.jd_parser import SUPPORTED_EXTENSIONS, extract_text
from models.jd import JDTextInput, JDUploadResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jd", tags=["jd"])


@router.post("/upload", response_model=JDUploadResponse)
async def upload_jd(file: UploadFile = File(...)):
    """
    POST /jd/upload  (multipart/form-data)
    ─────────────────────────────────────────────────────────────────
    Accepts a PDF, DOCX, or TXT file.
    Extracts plain text and returns it so the frontend can:
      1. Show a preview to the user
      2. Pass it to POST /call/start as jd_text
    """
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Please upload a PDF, DOCX, or TXT file.",
        )

    logger.info(f"JD upload received: {filename} ({file.content_type})")

    try:
        jd_text = await extract_text(file)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"JD extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to extract text from file.")

    if not jd_text.strip():
        raise HTTPException(
            status_code=422,
            detail="No readable text could be extracted from the file.",
        )

    logger.info(f"JD extracted: {len(jd_text.split())} words from {filename}")

    return JDUploadResponse(
        jd_text=jd_text,
        word_count=len(jd_text.split()),
        char_count=len(jd_text),
    )


@router.post("/text", response_model=JDUploadResponse)
async def paste_jd(body: JDTextInput):
    """
    POST /jd/text  (application/json)
    ─────────────────────────────────────────────────────────────────
    Accepts raw pasted JD text directly from the textarea.
    Returns the same shape as /jd/upload for a consistent frontend contract.
    """
    jd_text = body.text.strip()

    if not jd_text:
        raise HTTPException(status_code=400, detail="JD text cannot be empty.")

    if len(jd_text) < 50:
        raise HTTPException(
            status_code=400,
            detail="JD text is too short. Please provide the full job description.",
        )

    return JDUploadResponse(
        jd_text=jd_text,
        word_count=len(jd_text.split()),
        char_count=len(jd_text),
    )
