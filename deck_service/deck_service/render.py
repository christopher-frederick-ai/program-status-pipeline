"""Render a .pptx to PDF and page images using LibreOffice and poppler."""
import base64
import subprocess
from pathlib import Path


class RenderError(RuntimeError):
    pass


def render_pdf(pptx: Path, outdir: Path, timeout: int = 180) -> Path:
    """Convert deck.pptx to deck.pdf in outdir. Raises RenderError on failure."""
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(pptx)],
                       capture_output=True, timeout=timeout, check=True)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        raise RenderError(f"LibreOffice conversion failed: {e}") from e
    pdf = outdir / (pptx.stem + ".pdf")
    if not pdf.exists():
        raise RenderError("LibreOffice produced no PDF")
    return pdf


def page_images_b64(pdf: Path, workdir: Path, dpi: int = 60, max_pages: int = 12) -> list[str]:
    """Return base64 PNGs of the first pages, small enough to send back to the model."""
    prefix = workdir / "page"
    try:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-l", str(max_pages), str(pdf), str(prefix)],
                       capture_output=True, timeout=120, check=True)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        raise RenderError(f"pdftoppm failed: {e}") from e
    return [base64.standard_b64encode(p.read_bytes()).decode() for p in sorted(workdir.glob("page-*.png"))]
