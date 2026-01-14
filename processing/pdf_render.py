from pathlib import Path
import logging
from pdf2image import convert_from_path

logger = logging.getLogger(__name__)


def render_pdf_to_images(pdf_path, out_dir, dpi=300):
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(out_dir.glob("page_*.png"))
    if existing:
        logger.info("Using existing rendered images for %s", pdf_path)
        return [str(p) for p in existing]

    images = convert_from_path(str(pdf_path), dpi=dpi)
    output_paths = []
    for idx, image in enumerate(images, start=1):
        out_path = out_dir / f"page_{idx:03}.png"
        image.save(out_path, "PNG")
        output_paths.append(str(out_path))

    logger.info("Rendered %s pages for %s", len(output_paths), pdf_path)
    return output_paths
