"""
Generates synthetic 'product photography' for the dummy catalog: a plain
white studio background with a simple flat-icon illustration of the
product, so different products are visually distinguishable enough for a
real image-similarity check (not just random noise).

Run once: `python3 generate_images.py`
"""
import hashlib
import os

from PIL import Image, ImageDraw, ImageFilter

from products_data import PRODUCTS, CATEGORY_STYLE

OUT_DIR = os.path.join(os.path.dirname(__file__), "static", "images", "products")
SIZE = 600


def _variant_color(base, sku, spread=35):
    """Deterministic small color variation per SKU so items in the same
    category aren't pixel-identical."""
    h = int(hashlib.md5(sku.encode()).hexdigest(), 16)
    r = (h) % (spread * 2) - spread
    g = (h >> 8) % (spread * 2) - spread
    b = (h >> 16) % (spread * 2) - spread
    return tuple(max(0, min(255, c + d)) for c, d in zip(base, (r, g, b)))


def _shadow(draw, cx, cy, w, h):
    draw.ellipse([cx - w, cy - h * 0.25, cx + w, cy + h * 0.25], fill=(225, 225, 225))


def draw_cartridge(draw, color, accent):
    draw.rounded_rectangle([180, 160, 420, 420], radius=24, fill=color)
    draw.rectangle([210, 120, 390, 180], fill=color)
    draw.rounded_rectangle([200, 220, 400, 300], radius=10, fill=accent)
    draw.rectangle([260, 90, 340, 130], fill=(90, 90, 90))


def draw_cable(draw, color, accent):
    draw.line([150, 480, 250, 380, 350, 420, 450, 250], fill=color, width=26, joint="curve")
    draw.rounded_rectangle([110, 460, 170, 500], radius=8, fill=accent)
    draw.rounded_rectangle([430, 210, 490, 270], radius=8, fill=accent)


def draw_chair(draw, color, accent):
    draw.rounded_rectangle([180, 130, 420, 300], radius=40, fill=color)
    draw.rounded_rectangle([190, 310, 410, 400], radius=20, fill=color)
    draw.rectangle([290, 400, 310, 460], fill=(50, 50, 50))
    for dx in (-60, -20, 20, 60):
        draw.line([300, 460, 300 + dx, 510], fill=(50, 50, 50), width=8)
    draw.rounded_rectangle([170, 300, 220, 380], radius=10, fill=accent)
    draw.rounded_rectangle([380, 300, 430, 380], radius=10, fill=accent)


def draw_monitor(draw, color, accent):
    draw.rounded_rectangle([120, 130, 480, 380], radius=14, fill=color)
    draw.rectangle([140, 150, 460, 360], fill=accent)
    draw.rectangle([280, 380, 320, 430], fill=color)
    draw.rectangle([220, 430, 380, 450], fill=color)


def draw_printer(draw, color, accent):
    draw.rounded_rectangle([140, 260, 460, 420], radius=16, fill=color)
    draw.rectangle([180, 180, 420, 270], fill=color)
    draw.rectangle([220, 300, 380, 340], fill=accent)
    draw.ellipse([400, 380, 430, 410], fill=accent)


def draw_paper(draw, color, accent):
    for i, off in enumerate((30, 15, 0)):
        draw.rectangle([190 - off, 150 - off, 410 - off, 450 - off], fill=color, outline=accent, width=3)
    draw.rectangle([220, 190, 380, 200], fill=accent)
    draw.rectangle([220, 220, 380, 230], fill=accent)
    draw.rectangle([220, 250, 340, 260], fill=accent)


def draw_bottle(draw, color, accent):
    draw.rounded_rectangle([230, 200, 370, 440], radius=20, fill=color)
    draw.rectangle([260, 140, 340, 210], fill=color)
    draw.rounded_rectangle([255, 110, 345, 150], radius=8, fill=accent)
    draw.rounded_rectangle([240, 260, 360, 340], radius=8, fill=accent)


def draw_desk(draw, color, accent):
    draw.rectangle([120, 260, 480, 300], fill=color)
    draw.rectangle([150, 300, 180, 460], fill=accent)
    draw.rectangle([420, 300, 450, 460], fill=accent)
    draw.rectangle([230, 320, 320, 380], fill=(255, 255, 255))


def draw_stapler(draw, color, accent):
    draw.rounded_rectangle([160, 250, 440, 320], radius=20, fill=color)
    draw.polygon([(160, 250), (440, 250), (400, 190), (200, 190)], fill=color)
    draw.rounded_rectangle([190, 300, 250, 340], radius=6, fill=accent)


SHAPE_FN = {
    "cartridge": draw_cartridge,
    "cable": draw_cable,
    "chair": draw_chair,
    "monitor": draw_monitor,
    "printer": draw_printer,
    "paper": draw_paper,
    "bottle": draw_bottle,
    "desk": draw_desk,
    "stapler": draw_stapler,
}


def make_image(product):
    style = CATEGORY_STYLE[product["category"]]
    color = _variant_color(style["color"], product["sku"])
    accent = _variant_color(style["accent"], product["sku"] + "a")

    img = Image.new("RGB", (SIZE, SIZE), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    _shadow(draw, SIZE / 2, SIZE - 70, 160, 40)
    SHAPE_FN[style["shape"]](draw, color, accent)
    img = img.filter(ImageFilter.SMOOTH)
    return img


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in PRODUCTS:
        img = make_image(p)
        path = os.path.join(OUT_DIR, f"{p['sku']}.png")
        img.save(path, "PNG")
        print("wrote", path)


if __name__ == "__main__":
    main()
