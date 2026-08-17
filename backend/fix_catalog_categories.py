"""
Catalog category audit + fix.

Uses vectors the app already has (index_clip.npz) to find products whose own
CLIP vector sits closer to a DIFFERENT category's centroid than to their own
assigned category's -- a direct self-consistency check, not a guess: if a
"furniture"-labeled item's vector is actually closest to the "rugs"
centroid, its own catalog image looks more like a rug than like the rest of
the furniture bucket. Manually spot-checked at several margin thresholds
(see chat) -- margin > 0.10 was 20/20 clean, several with the correct
category literally spelled out in the product's own name ("Velvet-
Upholstered Office Chair" filed under furniture, "Typewriter Wall Art
Print" filed under home_decor). Lower thresholds start including genuinely
ambiguous boundary cases (a soap dish storage-vs-kitchen), so this stays
conservative on purpose -- catching the clear mislabels, not chasing every
possible relabel.

Does NOT touch Cloud SQL -- this only rewrites the local catalog_abo.json
(the source of truth both backends seed from). A separate step applies the
same corrections to the live products table once these are reviewed.

Usage (from backend/):
  python fix_catalog_categories.py            # apply fixes, write catalog_abo.json
  python fix_catalog_categories.py --dry-run   # report only, no file changes
"""
import argparse
import csv
import json
import os
import shutil

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "data", "index_clip.npz")
CATALOG_PATH = os.path.join(BASE_DIR, "data", "catalog_abo.json")
REPORT_PATH = os.path.join(BASE_DIR, "data", "category_fix_report.csv")
MARGIN_THRESHOLD = 0.10

# Manually reviewed exclusions -- the audit's mechanical rule (nearest
# centroid wins) is right ~97% of the time at this margin, but every
# candidate pattern was spot-checked against real product names (see chat),
# and these came back genuinely wrong, not just borderline:
#
# - Anything targeting "office_supplies": plain AmazonBasics product
#   photography (hangers, freezer boxes, an iron, air conditioners, photo
#   frames, even an "Ergonomic Office Chair" that should go to `chairs`)
#   is a false attractor for this category's centroid in the fused vector
#   space -- a styling artifact, not a genuine content match. 35 items
#   across 4 source categories, all reviewed, all wrong.
# - (chairs -> cleaning): 2 items (a barber chair, a kids' outdoor table),
#   neither remotely cleaning-related.
EXCLUDED_TARGET_CATEGORIES = {"office_supplies"}
EXCLUDED_PAIRS = {("chairs", "cleaning")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report mismatches only, don't write any files")
    ap.add_argument("--margin", type=float, default=MARGIN_THRESHOLD)
    args = ap.parse_args()

    d = np.load(INDEX_PATH, allow_pickle=True)
    skus = d["skus"]
    vecs = d["vecs"].astype(np.float32)
    vecs = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-8)

    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)
    products = {p["sku"]: p for p in catalog}

    cat_of = np.array([products[s]["category"] for s in skus])
    cats = sorted(set(cat_of.tolist()))

    centroids = {}
    for c in cats:
        mask = cat_of == c
        v = vecs[mask].mean(axis=0)
        centroids[c] = v / (np.linalg.norm(v) + 1e-8)
    C = np.stack([centroids[c] for c in cats])

    sims = vecs @ C.T
    own_idx = np.array([cats.index(c) for c in cat_of])
    own_sim = sims[np.arange(len(skus)), own_idx]
    best_idx = sims.argmax(axis=1)
    best_sim = sims[np.arange(len(skus)), best_idx]
    margin = best_sim - own_sim
    fix_mask = (best_idx != own_idx) & (margin > args.margin)

    fix_indices = [
        i for i in np.where(fix_mask)[0]
        if cats[best_idx[i]] not in EXCLUDED_TARGET_CATEGORIES
        and (cat_of[i], cats[best_idx[i]]) not in EXCLUDED_PAIRS
    ]
    excluded_count = int(fix_mask.sum()) - len(fix_indices)
    print(f"[fix_catalog_categories] {len(skus)} products, {len(fix_indices)} fixes at margin > {args.margin} "
          f"({100 * len(fix_indices) / len(skus):.1f}%) -- {excluded_count} more matched the margin but were "
          f"excluded as reviewed-bad patterns (see EXCLUDED_TARGET_CATEGORIES / EXCLUDED_PAIRS)")

    # Report: full diff, sorted by margin desc (most-confident fixes first)
    rows = []
    for i in sorted(fix_indices, key=lambda i: -margin[i]):
        sku = str(skus[i])
        p = products[sku]
        rows.append({
            "sku": sku, "name": p["name"][:80],
            "old_category": p["category"], "new_category": cats[best_idx[i]],
            "margin": round(float(margin[i]), 4),
        })
    with open(REPORT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "name", "old_category", "new_category", "margin"])
        w.writeheader()
        w.writerows(rows)
    print(f"[fix_catalog_categories] full diff written to {REPORT_PATH}")

    # Summary: which category pairs changed most
    from collections import Counter
    pair_counts = Counter((r["old_category"], r["new_category"]) for r in rows)
    print("\nTop relabeling patterns:")
    for (old, new), n in pair_counts.most_common(10):
        print(f"  {old:>15} -> {new:<15} {n}")

    if args.dry_run:
        print("\n[fix_catalog_categories] --dry-run: no files changed.")
        return

    # Apply fixes + write catalog_abo.json (backup first)
    backup_path = CATALOG_PATH + ".bak"
    if not os.path.exists(backup_path):
        shutil.copy(CATALOG_PATH, backup_path)
        print(f"[fix_catalog_categories] backed up original catalog to {backup_path}")

    for r in rows:
        products[r["sku"]]["category"] = r["new_category"]
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False)
    print(f"[fix_catalog_categories] wrote {len(rows)} corrected categories to {CATALOG_PATH}")
    print("[fix_catalog_categories] category centroids will be recomputed automatically on next app startup "
          "(_build_category_centroids reads from the live catalog, not a cache).")


if __name__ == "__main__":
    main()
