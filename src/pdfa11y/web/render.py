"""Image rendering for the web app: crops of figures and page thumbnails, cached on disk."""

from __future__ import annotations

import io
import threading
from pathlib import Path

from ..figures import FigureItem

_pdfium_lock = threading.Lock()          # pdfium is not thread-safe
SCALE = 1.6


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def figure_images(work: Path, fig: FigureItem, cache: Path) -> dict[str, Path]:
    """Render (once) and return {'crop', 'thumb', 'page'} PNG paths for a figure."""
    cache.mkdir(parents=True, exist_ok=True)
    paths = {k: cache / f"{fig.key}_{k}.png" for k in ("crop", "thumb", "page")}
    if all(p.exists() for p in paths.values()):
        return paths

    import pypdfium2 as pdfium
    from PIL import ImageDraw

    with _pdfium_lock:
        pdf = pdfium.PdfDocument(str(work))
        try:
            page = pdf[fig.page]
            left, bottom, right, top = page.get_mediabox()
            img = page.render(scale=SCALE).to_pil().convert("RGB")
        finally:
            pdf.close()
    W, H = img.size
    crop, rect = img, None
    if fig.bbox:
        x0, y0, x1, y1 = fig.bbox
        pad = 6
        px0, px1 = max(0, int((x0 - left - pad) * SCALE)), min(W, int((x1 - left + pad) * SCALE))
        py0, py1 = max(0, int((top - y1 - pad) * SCALE)), min(H, int((top - y0 + pad) * SCALE))
        if px1 - px0 > 4 and py1 - py0 > 4:
            crop, rect = img.crop((px0, py0, px1, py1)), (px0, py0, px1, py1)
    big = crop
    if max(big.size) > 900:
        k = 900 / max(big.size)
        big = big.resize((max(1, int(big.width * k)), max(1, int(big.height * k))))
    thumb = crop.copy()
    thumb.thumbnail((120, 120))
    pg = img.copy()
    if rect:
        ImageDraw.Draw(pg).rectangle(rect, outline=(220, 20, 60), width=6)
    k = 420 / pg.height
    pg = pg.resize((max(1, int(pg.width * k)), 420))
    paths["crop"].write_bytes(_png(big))
    paths["thumb"].write_bytes(_png(thumb))
    paths["page"].write_bytes(_png(pg))
    return paths


def page_image(work: Path, page_index: int, scale: float, cache: Path) -> Path:
    """A whole page as JPEG (smaller than PNG for pages with photos), rendered once per scale and cached."""
    scale = max(0.5, min(3.0, round(scale * 4) / 4))                     # steps of 0.25 so the cache is reused
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"page_{page_index}_{int(scale * 100)}.jpg"
    if out.exists():
        return out
    import pypdfium2 as pdfium

    with _pdfium_lock:
        pdf = pdfium.PdfDocument(str(work))
        try:
            img = pdf[page_index].render(scale=scale).to_pil().convert("RGB")
        finally:
            pdf.close()
    tmp = out.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=85, optimize=True)
    tmp.replace(out)
    return out
