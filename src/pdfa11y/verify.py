"""Safety gates run on tagged output before it is accepted."""

from __future__ import annotations

from pathlib import Path


def render_diff(original: str | Path, result: str | Path, scale: float = 0.4) -> list[int]:
    """1-based page numbers whose rendering differs between the two files (empty = identical)."""
    import pypdfium2 as pdfium
    from PIL import ImageChops

    a, b = pdfium.PdfDocument(str(original)), pdfium.PdfDocument(str(result))
    try:
        if len(a) != len(b):
            return list(range(1, max(len(a), len(b)) + 1))
        bad = []
        for i in range(len(a)):
            ia = a[i].render(scale=scale).to_pil().convert("RGB")
            ib = b[i].render(scale=scale).to_pil().convert("RGB")
            if ImageChops.difference(ia, ib).getbbox():
                bad.append(i + 1)
        return bad
    finally:
        a.close()
        b.close()
