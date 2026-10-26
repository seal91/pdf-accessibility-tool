"""Shared test helpers. The tests build their own small PDFs, so they need none of the real publications."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Dictionary, Name

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

BODY = ("This paragraph exists so the tests have something to tag. It is ordinary body text of the kind a report "
        "contains, repeated over several lines so the layout analysis can tell a heading from a paragraph.")


def make_pdf(path: Path, with_image: bool = True) -> Path:
    """A one-page PDF: a heading, six lines of body text and (optionally) a picture."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(595, 842))
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    res = Dictionary(Font=Dictionary(F1=font))
    content = b"BT /F1 26 Tf 72 780 Td (Test document heading) Tj ET\n"
    y = 730
    for _ in range(6):
        content += b"BT /F1 11 Tf 72 " + str(y).encode() + b" Td (" + BODY.encode() + b") Tj ET\n"
        y -= 16
    if with_image:
        rgb = bytes([200, 30, 30, 30, 200, 30, 30, 30, 200, 220, 220, 40] * 4)         # 4x4 pixels
        img = pdf.make_stream(rgb)
        img.Type, img.Subtype, img.Width, img.Height = Name.XObject, Name.Image, 4, 4
        img.ColorSpace, img.BitsPerComponent = Name.DeviceRGB, 8
        res.XObject = Dictionary(Im1=img)
        content += b"q 200 0 0 150 72 400 cm /Im1 Do Q\n"
    page.Resources = res
    page.Contents = pdf.make_stream(content)
    pdf.save(path)
    return path


@pytest.fixture()
def test_pdf(tmp_path) -> Path:
    return make_pdf(tmp_path / "test document.pdf")


def wait_ready(c, doc_id: str, timeout: int = 180) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        d = c.get(f"/api/documents/{doc_id}").json()
        if d["document"]["status"] != "processing":
            return d
        time.sleep(0.5)
    raise TimeoutError("document did not finish processing")
