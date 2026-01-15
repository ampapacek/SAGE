import logging
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)


def extract_pdf_text(pdf_path, out_dir):
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path = out_dir / "text.txt"

    cached_text = ""
    cached = False
    if text_path.exists():
        try:
            cached_text = text_path.read_text(errors="ignore")
            cached = True
        except OSError:
            cached_text = ""
            cached = False

    page_texts = []
    pages_with_text = 0
    image_count = 0
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                image_count += len(page.images or [])
                text = page.extract_text() or ""
                if text.strip():
                    pages_with_text += 1
                page_texts.append(text)
    except Exception:
        logger.exception("Failed extracting text from %s", pdf_path)
        raise

    combined = "\n\n".join([t for t in page_texts if t])
    if not cached:
        try:
            text_path.write_text(combined, encoding="utf-8")
        except OSError:
            logger.exception("Failed writing extracted text for %s", pdf_path)

    stats = {
        "pages": len(page_texts),
        "pages_with_text": pages_with_text,
        "total_chars": len(combined),
        "cached": cached,
        "image_count": image_count,
    }
    return combined, stats
