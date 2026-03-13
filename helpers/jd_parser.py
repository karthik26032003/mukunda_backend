import io
import pdfplumber
from docx import Document
from fastapi import UploadFile


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}


async def extract_text(file: UploadFile) -> str:
    """
    Read an uploaded file and return its plain text content.
    Supports: PDF (pdfplumber), DOCX (python-docx), TXT (utf-8).
    """
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '{ext}'. Allowed: PDF, DOCX, TXT")

    content = await file.read()

    if ext == ".pdf":
        return _parse_pdf(content)
    elif ext == ".docx":
        return _parse_docx(content)
    else:
        return content.decode("utf-8", errors="replace").strip()


def _parse_pdf(content: bytes) -> str:
    """Extract text from a PDF using pdfplumber (handles multi-column layouts)."""
    pages = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())

    if not pages:
        raise ValueError("No readable text found in PDF. It may be a scanned image.")

    return "\n\n".join(pages)


def _parse_docx(content: bytes) -> str:
    """Extract text from a DOCX file using python-docx."""
    doc = Document(io.BytesIO(content))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    if not paragraphs:
        raise ValueError("No text found in DOCX file.")

    return "\n".join(paragraphs)
