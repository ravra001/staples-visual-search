"""
Ingest a ~10k product catalog with REAL names + REAL images from the
Amazon Berkeley Objects (ABO) open dataset.

Demo/hackathon use only — ABO images are licensed CC BY-NC 4.0 (non-commercial).
Source: https://amazon-berkeley-objects.s3.amazonaws.com/

Two phases:
  1. build   — parse the (already-downloaded) ABO metadata, select a balanced
               ~10k subset across store categories, synthesize commerce fields
               (price/rating/reviews), and write backend/data/catalog_abo.json.
  2. images  — download the selected products' images (small/256px) into
               static/images/products/<SKU>.jpg (skips ones already present).

Usage (from backend/):
    python ingest_abo.py build  --abo-dir <dir-with-listings_*.json.gz+images.csv.gz>
    python ingest_abo.py images --workers 32

Then run the app against it:
    CATALOG_FILE=data/catalog_abo.json python -m uvicorn main:app --port 8000
"""
import argparse
import csv
import glob
import gzip
import hashlib
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:  # trust the OS cert store (corporate TLS interception) — see embeddings.py
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images", "products")
CATALOG_PATH = os.path.join(DATA_DIR, "catalog_abo.json")
S3 = "https://amazon-berkeley-objects.s3.amazonaws.com"

TARGET = 10000

# ABO product_type  ->  our store category
CATEGORY_MAP = {
    "CHAIR": "chairs", "STOOL_SEATING": "chairs",
    "SOFA": "sofas", "OTTOMAN": "sofas",
    "TABLE": "tables",
    "DESK": "desks",
    "SHELF": "storage", "CABINET": "storage", "STORAGE_RACK": "storage",
    "HOME_FURNITURE_AND_DECOR": "furniture",
    "HOME": "home_decor", "PILLOW": "home_decor", "PLANTER": "home_decor",
    "WALL_ART": "wall_art",
    "RUG": "rugs",
    "LAMP": "lighting", "LIGHT_FIXTURE": "lighting", "LIGHT_BULB": "lighting",
    "OFFICE_PRODUCTS": "office_supplies",
    "JANITORIAL_SUPPLY": "cleaning",
    "KITCHEN": "kitchen", "DRINKING_CUP": "kitchen", "COFFEE": "kitchen",
}

# realistic price band per category: (low, high)
PRICE_BAND = {
    "chairs": (49, 340), "sofas": (240, 1200), "tables": (70, 620), "desks": (110, 700),
    "storage": (35, 400), "furniture": (30, 500), "home_decor": (12, 180),
    "wall_art": (18, 220), "rugs": (30, 420), "lighting": (18, 260),
    "office_supplies": (5, 90), "cleaning": (4, 45), "kitchen": (8, 130),
}


def _en(value):
    """Pick the English value from ABO's list-of-language-variants fields."""
    if isinstance(value, list):
        for e in value:
            if isinstance(e, dict) and str(e.get("language_tag", "")).startswith("en"):
                return e.get("value")
        return None
    return value


def _h(s):
    return int(hashlib.md5(s.encode()).hexdigest(), 16)


def _synth_commerce(item_id, category):
    """Deterministic price / rating / reviews from the item id."""
    h = _h(item_id)
    lo, hi = PRICE_BAND.get(category, (10, 100))
    price = round(lo + (h % 100000) / 100000 * (hi - lo), 2)
    price = round(price) - 0.01 if price > 5 else round(price, 2)  # .99 styling
    markup = 1.12 + (h >> 8) % 40 / 100.0                          # 1.12–1.52
    list_price = round(price * markup) - 0.01
    rating = round(3.8 + (h >> 16) % 12 / 10.0, 1)                 # 3.8–4.9
    rating = min(rating, 5.0)
    reviews = 5 + (h >> 20) % 4800
    return price, list_price, rating, reviews


def build(abo_dir):
    os.makedirs(DATA_DIR, exist_ok=True)

    # image_id -> relative path (e.g. "14/14fe8812.jpg")
    img_map = {}
    with gzip.open(os.path.join(abo_dir, "images.csv.gz"), "rt") as f:
        for row in csv.DictReader(f):
            img_map[row["image_id"]] = row["path"]
    print(f"[build] image index: {len(img_map)} images")

    # collect candidates per category
    buckets = {}
    scanned = 0
    for fp in sorted(glob.glob(os.path.join(abo_dir, "listings_*.json.gz"))):
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            for line in f:
                scanned += 1
                o = json.loads(line)
                pt = o.get("product_type")
                pt = pt[0]["value"] if isinstance(pt, list) and pt else None
                cat = CATEGORY_MAP.get(pt)
                if not cat:
                    continue
                name = _en(o.get("item_name"))
                img_id = o.get("main_image_id")
                if not name or not img_id or img_id not in img_map:
                    continue
                buckets.setdefault(cat, []).append((o, name, img_id))
    print(f"[build] scanned {scanned} listings; candidates by category:")
    for c in sorted(buckets):
        print(f"         {len(buckets[c]):6d}  {c}")

    # deterministic shuffle within each category, then round-robin to TARGET
    for c in buckets:
        buckets[c].sort(key=lambda t: _h(t[0]["item_id"]))
    cats = sorted(buckets, key=lambda c: -len(buckets[c]))
    selected = []
    idx = {c: 0 for c in cats}
    while len(selected) < TARGET and any(idx[c] < len(buckets[c]) for c in cats):
        for c in cats:
            if idx[c] < len(buckets[c]):
                selected.append((c, *buckets[c][idx[c]]))
                idx[c] += 1
                if len(selected) >= TARGET:
                    break

    products = []
    for cat, o, name, img_id in selected:
        item_id = o["item_id"]
        sku = f"ABO-{item_id}"
        brand = _en(o.get("brand")) or "Staples"
        bullets = o.get("bullet_point") or []
        desc = " ".join(b.get("value", "") for b in bullets
                        if isinstance(b, dict) and str(b.get("language_tag", "")).startswith("en"))
        price, list_price, rating, reviews = _synth_commerce(item_id, cat)
        products.append({
            "sku": sku,
            "name": name.strip()[:140],
            "brand": str(brand).strip()[:60],
            "category": cat,
            "price": price,
            "list_price": list_price,
            "rating": rating,
            "reviews": reviews,
            "description": desc.strip()[:400],
            "image_url": f"/images/products/{sku}.jpg",
            "_abo_path": img_map[img_id],  # consumed by the images phase, then dropped
        })

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(products, f)
    print(f"[build] wrote {len(products)} products -> {CATALOG_PATH}")


def _download_one(args):
    path, dest = args
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True
    url = f"{S3}/images/small/{path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def images(workers):
    with open(CATALOG_PATH, encoding="utf-8") as f:
        products = json.load(f)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    jobs = [(p["_abo_path"], os.path.join(IMAGES_DIR, f"{p['sku']}.jpg")) for p in products]
    ok = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_download_one, j) for j in jobs]
        for fut in as_completed(futs):
            done += 1
            ok += 1 if fut.result() else 0
            if done % 500 == 0:
                print(f"[images] {done}/{len(jobs)} ({ok} ok)")
    print(f"[images] complete: {ok}/{len(jobs)} downloaded")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["build", "images"])
    ap.add_argument("--abo-dir", default=os.path.join(DATA_DIR, "abo"))
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args()
    if args.phase == "build":
        build(args.abo_dir)
    else:
        images(args.workers)


if __name__ == "__main__":
    main()
