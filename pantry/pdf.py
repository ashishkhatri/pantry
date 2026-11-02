"""Receipt PDFs (the kind stores email or text you).

Most e-receipt PDFs contain real text, so we read it directly and give the AI the text;
that's more accurate than a photo. Scanned PDFs are only pictures, so their pages are
rendered to images for the AI's vision model instead.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import List

MAX_PAGES = 4


class PdfError(ValueError):
    pass


def _need(module: str):
    try:
        return __import__(module)
    except ImportError as e:
        raise PdfError(f"PDF receipts need the '{module}' package. Run the update (pantry-update) to install it.") from e


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def text(path: Path) -> str:
    """The text layer of the first few pages ('' for a scanned PDF)."""
    pypdf = _need("pypdf")
    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        parts = [(page.extract_text() or "") for page in reader.pages[:MAX_PAGES]]
    except Exception as e:  # damaged or password-protected file
        raise PdfError(f"Couldn't open that PDF ({e}).") from e
    return "\n".join(p.strip() for p in parts if p.strip())


def page_images(path: Path, long_side: int = 1800) -> List[bytes]:
    """JPEGs of the first few pages, for a scanned PDF."""
    pdfium = _need("pypdfium2")
    _need("PIL")
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:
        raise PdfError(f"Couldn't open that PDF ({e}).") from e
    out = []
    try:
        for i in range(min(len(doc), MAX_PAGES)):
            page = doc[i]
            w, h = page.get_size()
            img = page.render(scale=max(1.0, long_side / max(w, h))).to_pil().convert("L")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            out.append(buf.getvalue())
    finally:
        doc.close()
    return out
