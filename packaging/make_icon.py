"""Generate the app icon (app.ico + a PNG used for the window icon)."""
from pathlib import Path
from PIL import Image, ImageDraw

def draw(size: int) -> Image.Image:
    s = size * 4                                            # draw large, then downsample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=(29, 78, 216, 255))
    # page with folded corner
    x0, y0, x1, y1 = int(s * .27), int(s * .16), int(s * .73), int(s * .84)
    fold = int(s * .14)
    d.polygon([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)], fill=(255, 255, 255, 255))
    d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=(191, 207, 245, 255))
    # tick mark
    w = int(s * .055)
    d.line([(int(s * .36), int(s * .55)), (int(s * .47), int(s * .66)), (int(s * .64), int(s * .40))],
           fill=(29, 78, 216, 255), width=w, joint="curve")
    return img.resize((size, size), Image.LANCZOS)

here = Path(__file__).parent
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [draw(n) for n in sizes]
imgs[-1].save(here / "app.ico", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
draw(256).save(here.parent / "src" / "pdfa11y" / "gui" / "icon.png")
print("icon written")
