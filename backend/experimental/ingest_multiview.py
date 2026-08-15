"""
EXPERIMENT — multi-angle catalog ingestion from Amazon Berkeley Objects.

Downloads a subset of products that have several images (main_image_id +
other_image_id) so we can test cross-angle retrieval. Kept fully separate from
the production pipeline (ingest_abo.py) — writes its own catalog + image folder.

Usage (from backend/):
    python experimental/ingest_multiview.py --abo-dir <dir-with-listings_*.json.gz> \
        --n 500 --views 3

Outputs:
    data/catalog_multiview.json               list of {sku, name, category, views:[files]}
    static/images/multiview/<sku>__<i>.jpg    main (i=0) + other views
"""
import argparse
import csv
import glob
import gzip
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
from ingest_abo import CATEGORY_MAP, _en, _h   # reuse the production mapping/helpers

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BACKEND, "data")
IMG_DIR = os.path.join(BACKEND, "static", "images", "multiview")
CATALOG = os.path.join(DATA_DIR, "catalog_multiview.json")
S3 = "https://amazon-berkeley-objects.s3.amazonaws.com"


def build(abo_dir, n, views, min_others):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMG_DIR, exist_ok=True)

    img_map = {}
    with gzip.open(os.path.join(abo_dir, "images.csv.gz"), "rt") as f:
        for row in csv.DictReader(f):
            img_map[row["image_id"]] = row["path"]

    # collect eligible products: in-scope category, English name, main + >= min_others extras
    cands = []
    for fp in sorted(glob.glob(os.path.join(abo_dir, "listings_*.json.gz"))):
        with gzip.open(fp, "rt", encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                pt = o.get("product_type")
                pt = pt[0]["value"] if isinstance(pt, list) and pt else None
                cat = CATEGORY_MAP.get(pt)
                name = _en(o.get("item_name"))
                main = o.get("main_image_id")
                others = [i for i in (o.get("other_image_id") or []) if i in img_map]
                if not cat or not name or not main or main not in img_map:
                    continue
                if len(others) < min_others:
                    continue
                cands.append((o["item_id"], name, cat, main, others))

    cands.sort(key=lambda t: _h(t[0]))          # deterministic subset
    cands = cands[:n]

    products, jobs = [], []
    for item_id, name, cat, main, others in cands:
        sku = f"MV-{item_id}"
        image_ids = [main] + others[:views]     # main is view 0
        files = []
        for i, iid in enumerate(image_ids):
            fname = f"{sku}__{i}.jpg"
            files.append(fname)
            jobs.append((img_map[iid], os.path.join(IMG_DIR, fname)))
        products.append({"sku": sku, "name": name.strip()[:140], "category": cat, "views": files})

    with open(CATALOG, "w", encoding="utf-8") as f:
        json.dump(products, f)
    print(f"[ingest] {len(products)} products, {len(jobs)} view images -> downloading…")
    _download(jobs)
    print(f"[ingest] wrote {CATALOG}")


def _one(args):
    path, dest = args
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True
    try:
        with urllib.request.urlopen(f"{S3}/images/small/{path}", timeout=30) as r:
            data = r.read()
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _download(jobs, workers=40):
    ok = done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(_one, j) for j in jobs]):
            done += 1
            ok += 1 if fut.result() else 0
            if done % 500 == 0:
                print(f"[ingest] {done}/{len(jobs)} ({ok} ok)")
    print(f"[ingest] images: {ok}/{len(jobs)} downloaded")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--abo-dir", required=True)
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--views", type=int, default=3, help="max OTHER views per product (plus the main)")
    ap.add_argument("--min-others", type=int, default=2, help="require at least this many other views")
    a = ap.parse_args()
    build(a.abo_dir, a.n, a.views, a.min_others)
