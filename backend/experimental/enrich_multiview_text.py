"""
EXPERIMENT — add brand + description text to the multi-view catalog subset,
so we can test image+text fusion (per Amazon's published approach: catalog
vectors = fused image+text; query stays pure image).

Usage (from backend/):
    python experimental/enrich_multiview_text.py --abo-dir <dir-with-listings_*.json.gz>

Output: data/catalog_multiview_text.json  (catalog_multiview.json + brand/description)
"""
import argparse
import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest_abo import _en

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_IN = os.path.join(BACKEND, "data", "catalog_multiview.json")
CATALOG_OUT = os.path.join(BACKEND, "data", "catalog_multiview_text.json")


def main(abo_dir):
    with open(CATALOG_IN, encoding="utf-8") as f:
        products = json.load(f)
    want = {p["sku"][3:] for p in products}  # "MV-<item_id>" -> item_id

    text_by_id = {}
    for fp in sorted(glob.glob(os.path.join(abo_dir, "listings_*.json.gz"))):
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                item_id = o.get("item_id")
                if item_id not in want:
                    continue
                brand = _en(o.get("brand")) or ""
                bullets = o.get("bullet_point") or []
                desc = " ".join(
                    b.get("value", "") for b in bullets
                    if isinstance(b, dict) and str(b.get("language_tag", "")).startswith("en")
                )
                text_by_id[item_id] = (str(brand).strip(), desc.strip()[:300])

    n_found = 0
    for p in products:
        item_id = p["sku"][3:]
        brand, desc = text_by_id.get(item_id, ("", ""))
        p["brand"] = brand
        p["description"] = desc
        if brand or desc:
            n_found += 1

    with open(CATALOG_OUT, "w", encoding="utf-8") as f:
        json.dump(products, f)
    print(f"[enrich] {n_found}/{len(products)} products got brand/description text -> {CATALOG_OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--abo-dir", required=True)
    main(ap.parse_args().abo_dir)
