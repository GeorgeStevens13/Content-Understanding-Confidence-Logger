r"""Generate sample PDFs (and a couple of edge-case files) for end-to-end
testing of the pre-process quality checker (``src/quality_check.py``).

Run:
    python generate_sample_pdfs.py
    python generate_sample_pdfs.py --out samples --pages-overlimit 301

Then exercise the in-process checker against every sample, for example:
    from pathlib import Path
    import sys; sys.path.insert(0, "../src")
    from quality_check import check_document
    for pdf in Path("samples").glob("*.pdf"):
        report = check_document(str(pdf))
        print(pdf.name, "->", report.band, report.score, "passed=", report.passed)

Or upload them to the ``incoming/`` container and let the deployed Function
app pick them up via the ``PREPROCESS_SCHEDULE`` timer.

Each file is named so the expected result is obvious:
    pass_*  -> checker should report PASSED with no ERROR-severity issues
    fail_*  -> checker should report FAILED (at least one ERROR)
    warn_*  -> checker should report PASSED but raise a WARNING

Requires PyMuPDF: pip install pymupdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.stderr.write(
        "PyMuPDF is required. Install with:  python -m pip install pymupdf\n"
    )
    sys.exit(2)


def _write_text_pdf(path: Path, pages: int, text_per_page: str) -> None:
    """Create a real, text-extractable PDF.

    Uses ``insert_textbox`` so text wraps inside the page margins instead of
    being clipped at the right edge by a single-line ``insert_text`` call.
    """
    doc = fitz.open()
    try:
        # Letter page is 612 x 792 pt; leave 1-inch (72pt) margins.
        textbox = fitz.Rect(72, 72, 540, 720)
        for i in range(pages):
            page = doc.new_page()
            page.insert_textbox(
                textbox,
                f"Page {i + 1} of {pages}\n\n{text_per_page}",
                fontsize=11,
            )
        doc.save(path)
    finally:
        doc.close()


def _write_image_only_pdf(path: Path, pages: int) -> None:
    """Create a PDF whose pages contain only a rendered image (no extractable text).

    The checker samples the first few pages; if it finds almost no text it raises
    ``PDF_LIKELY_SCANNED``. We produce that by drawing shapes instead of text.
    """
    doc = fitz.open()
    try:
        for _ in range(pages):
            page = doc.new_page()
            # Draw a couple of filled rectangles to mimic a scanned page bitmap.
            page.draw_rect(fitz.Rect(72, 72, 540, 200), fill=(0.85, 0.85, 0.85))
            page.draw_rect(fitz.Rect(72, 220, 540, 720), fill=(0.95, 0.95, 0.95))
        doc.save(path)
    finally:
        doc.close()


def _write_sparse_text_pdf(path: Path, pages: int, dense_pages: int) -> None:
    """PDF with a couple of normal pages and many nearly-empty ones.

    Triggers ``PDF_LOW_TEXT_DENSITY`` (low avg chars/page) and
    ``PDF_MANY_LOW_TEXT_PAGES`` (many pages below the low-text threshold).
    """
    dense_text = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 25)
    textbox = fitz.Rect(72, 72, 540, 720)
    doc = fitz.open()
    try:
        for i in range(pages):
            page = doc.new_page()
            if i < dense_pages:
                page.insert_textbox(textbox, dense_text, fontsize=11)
            else:
                # Just a page number; well below LOW_TEXT_CHAR_THRESHOLD (50).
                page.insert_text((72, 72), f"{i + 1}", fontsize=10)
        doc.save(path)
    finally:
        doc.close()


def _write_encrypted_pdf(path: Path, password: str = "secret") -> None:
    """Create a password-protected PDF (AES-256)."""
    doc = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "This PDF is password protected.", fontsize=12)
        doc.save(
            path,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=password,
            user_pw=password,
            permissions=fitz.PDF_PERM_ACCESSIBILITY,
        )
    finally:
        doc.close()


def _write_corrupt_pdf(path: Path) -> None:
    """Write bytes that look like a PDF header but are not a valid document."""
    path.write_bytes(b"%PDF-1.7\n%garbage-not-a-real-pdf\n%%EOF\n")


def _write_renamed_text(path: Path) -> None:
    """Write a plain-text file using a .pdf extension to trigger MAGIC_MISMATCH."""
    path.write_text("This is not a PDF, just text saved with a .pdf extension.\n",
                    encoding="utf-8")


def _write_empty(path: Path) -> None:
    path.write_bytes(b"")


def _render_text_page_image(width: int, height: int, page_num: int):
    """Build a PIL ``Image`` of a synthetic letter-size text page.

    Shared helper used by the smudged / skewed / low-DPI scan fixtures.
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()

    body_paragraph = (
        "This page is a synthetic scan used by the document quality checker "
        "test suite. Real scans of this quality cause OCR to misread or skip "
        "text entirely. The pre-process checker should flag this page and "
        "recommend rescanning before sending it to Content Understanding.\n\n"
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do "
        "eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim "
        "ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut "
        "aliquip ex ea commodo consequat.\n\n"
    )

    img = Image.new("RGB", (width, height), color=(245, 245, 240))
    draw = ImageDraw.Draw(img)
    full_text = f"Page {page_num}\n\n" + (body_paragraph * 8)
    draw.multiline_text(
        (60, 60),
        full_text,
        fill=(0, 0, 0),
        font=font,
        spacing=6,
    )
    return img


def _write_smudged_scan_pdf(path: Path, pages: int) -> None:
    """PDF made of synthetically blurred raster pages, mimicking a smudged scan.

    Each page is built as a high-resolution image of text, then heavily blurred
    with a Gaussian filter, and finally embedded as the only content on a PDF
    page. Triggers ``PDF_LIKELY_SCANNED`` plus ``PDF_SCAN_BLURRY``.
    """
    import io
    try:
        from PIL import ImageFilter
    except ImportError:
        sys.stderr.write(
            "Pillow is required for the smudged-scan fixture. "
            "Install with: python -m pip install pillow\n"
        )
        raise

    doc = fitz.open()
    try:
        for i in range(pages):
            # ~150 DPI letter page. Strong text/background contrast keeps the
            # post-blur stdev above PDF_SCAN_LOW_CONTRAST_STDEV so the page is
            # classified as blurry (rather than low-contrast / blank).
            img = _render_text_page_image(1275, 1650, i + 1)
            # Heavy Gaussian blur -> rendered Laplacian variance drops below
            # PDF_SCAN_BLUR_VARIANCE_MIN while contrast stays above the floor.
            img = img.filter(ImageFilter.GaussianBlur(radius=8))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            page = doc.new_page()
            page.insert_image(page.rect, stream=img_bytes)
        doc.save(path)
    finally:
        doc.close()


def _write_skewed_scan_pdf(path: Path, pages: int, angle_deg: float = 5.0
                           ) -> None:
    """PDF whose pages are crisp text rotated by ``angle_deg`` and embedded
    as raster images. Triggers ``PDF_LIKELY_SCANNED`` + ``PDF_SCAN_SKEWED``.
    """
    import io
    from PIL import Image

    doc = fitz.open()
    try:
        for i in range(pages):
            img = _render_text_page_image(1275, 1650, i + 1)
            # Rotate clockwise/counter-clockwise; fill exposed corners white.
            img = img.rotate(angle_deg, resample=Image.BICUBIC,
                             fillcolor=(245, 245, 240), expand=False)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            page = doc.new_page()
            page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    finally:
        doc.close()


def _write_low_dpi_scan_pdf(path: Path, pages: int) -> None:
    """PDF that embeds a small raster image (~70 DPI) stretched to fill the
    letter page. Triggers ``PDF_LIKELY_SCANNED`` + ``PDF_SCAN_LOW_DPI``.
    """
    import io

    doc = fitz.open()
    try:
        for i in range(pages):
            # Render at low resolution so the embedded image is small.
            # Stretched across an 8.5x11" page this is ~70 DPI.
            img = _render_text_page_image(600, 800, i + 1)
            buf = io.BytesIO()
            # Use JPEG with mild compression to look like a real scan.
            img.save(buf, format="JPEG", quality=60)
            page = doc.new_page()
            page.insert_image(page.rect, stream=buf.getvalue())
        doc.save(path)
    finally:
        doc.close()


def generate(out_dir: Path, pages_overlimit: int) -> list[tuple[str, Path]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, Path]] = []

    # ---- pass cases ---------------------------------------------------------
    small = out_dir / "pass_small_text.pdf"
    _write_text_pdf(small, pages=3,
                    text_per_page="Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 15)
    files.append(("PASS", small))

    medium = out_dir / "pass_medium_text.pdf"
    _write_text_pdf(medium, pages=25,
                    text_per_page="The quick brown fox jumps over the lazy dog. " * 25)
    files.append(("PASS", medium))

    # ---- warn cases ---------------------------------------------------------
    scanned = out_dir / "warn_scanned_like.pdf"
    _write_image_only_pdf(scanned, pages=4)
    files.append(("WARN (PDF_LIKELY_SCANNED)", scanned))

    sparse = out_dir / "warn_sparse_text.pdf"
    _write_sparse_text_pdf(sparse, pages=10, dense_pages=2)
    files.append(("WARN (PDF_LOW_TEXT_DENSITY + PDF_MANY_LOW_TEXT_PAGES)", sparse))

    smudged = out_dir / "warn_smudged_scan.pdf"
    _write_smudged_scan_pdf(smudged, pages=4)
    files.append(("WARN (PDF_LIKELY_SCANNED + PDF_SCAN_BLURRY)", smudged))

    skewed = out_dir / "warn_skewed_scan.pdf"
    _write_skewed_scan_pdf(skewed, pages=4, angle_deg=5.0)
    files.append(("WARN (PDF_LIKELY_SCANNED + PDF_SCAN_SKEWED)", skewed))

    low_dpi = out_dir / "warn_low_dpi_scan.pdf"
    _write_low_dpi_scan_pdf(low_dpi, pages=4)
    files.append(("WARN (PDF_LIKELY_SCANNED + PDF_SCAN_LOW_DPI)", low_dpi))

    renamed = out_dir / "fail_renamed_text.pdf"
    _write_renamed_text(renamed)
    # Triggers MAGIC_MISMATCH (warning) AND PDF_OPEN_FAILED (error) because
    # PyMuPDF still attempts to parse it as a PDF, so this case fails overall.
    files.append(("FAIL (MAGIC_MISMATCH + PDF_OPEN_FAILED)", renamed))

    # ---- fail cases ---------------------------------------------------------
    empty = out_dir / "fail_empty.pdf"
    _write_empty(empty)
    files.append(("FAIL (FILE_EMPTY)", empty))

    corrupt = out_dir / "fail_corrupt.pdf"
    _write_corrupt_pdf(corrupt)
    files.append(("FAIL (PDF_OPEN_FAILED)", corrupt))

    encrypted = out_dir / "fail_encrypted.pdf"
    _write_encrypted_pdf(encrypted)
    files.append(("FAIL (PDF_PASSWORD_PROTECTED)", encrypted))

    if pages_overlimit > 300:
        too_many = out_dir / f"fail_too_many_pages_{pages_overlimit}.pdf"
        _write_text_pdf(too_many, pages=pages_overlimit,
                        text_per_page="x")
        files.append((f"FAIL (PDF_TOO_MANY_PAGES, {pages_overlimit} pages)", too_many))

    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="samples",
                        help="Output directory (default: ./samples)")
    parser.add_argument("--pages-overlimit", type=int, default=301,
                        help=("Generate an oversized PDF with this many pages "
                              "to trigger PDF_TOO_MANY_PAGES. Set to 0 to skip "
                              "(default: 301)."))
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    print(f"Generating samples in: {out}\n")
    results = generate(out, pages_overlimit=args.pages_overlimit)

    width = max(len(label) for label, _ in results)
    for label, path in results:
        size_kb = path.stat().st_size / 1024
        print(f"  {label:<{width}}  {path.name:<40}  {size_kb:8.1f} KB")

    print("\nNext step — upload to incoming/ and watch the preprocess timer:")
    print(f"  az storage blob upload-batch -d incoming/demo/generic-doc-analyzer \\\n      -s \"{out}\" --account-name <storage> --auth-mode login")
    return 0


if __name__ == "__main__":
    sys.exit(main())
