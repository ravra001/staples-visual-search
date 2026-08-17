"""
Apply the local category corrections (see fix_catalog_categories.py) to the
live Cloud SQL products table, and regenerate category_centroids from the
corrected groupings.

fix_catalog_categories.py only rewrites the local catalog_abo.json -- Cloud
SQL has its own already-seeded copy of `category` that a local file edit
never touches. This is that separate step. Only the `category` column (and
the derived centroids) change; embeddings, prices, everything else is
untouched.

Run from Cloud Shell (DATA_BACKEND=sql, a real DATABASE_URL):

  cd ~/staples-images-src/backend   # or wherever the repo is cloned
  git pull                          # get the corrected catalog_abo.json
  export DATA_BACKEND=sql
  export DATABASE_URL=postgresql+psycopg://...
  export CATALOG_FILE=data/catalog_abo.json
  python apply_category_corrections.py --dry-run   # see the diff first
  python apply_category_corrections.py             # apply
"""
import argparse

import config
import products_repo_sql as repo
from products_data import load_catalog_file, catalog_content_hash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report the diff only, don't write anything")
    args = ap.parse_args()

    if config.DATA_BACKEND != "sql":
        raise RuntimeError("This script updates Cloud SQL -- set DATA_BACKEND=sql (and a real DATABASE_URL) first.")

    catalog = load_catalog_file(config.CATALOG_FILE)
    correct = {p["sku"]: p["category"] for p in catalog}

    # One query for the whole table's current category, not one per sku --
    # 10k individual round trips against remote Cloud SQL is exactly the N+1
    # mistake already documented (and fixed) in init_and_seed().
    with repo.SessionLocal() as s:
        current = dict(s.query(repo.Product.sku, repo.Product.category).all())

    diffs = [{"b_sku": sku, "category": cat} for sku, cat in correct.items()
              if sku in current and current[sku] != cat]

    print(f"[apply_category_corrections] {len(current)} rows in Cloud SQL, {len(diffs)} need a category update")
    if not diffs:
        print("[apply_category_corrections] nothing to do -- Cloud SQL already matches the local catalog.")
        return

    if args.dry_run:
        for d in diffs[:20]:
            print(f"  {d['b_sku']}: -> {d['category']}")
        if len(diffs) > 20:
            print(f"  ... and {len(diffs) - 20} more")
        print("\n[apply_category_corrections] --dry-run: no changes made.")
        return

    from sqlalchemy import bindparam
    stmt = (
        repo.Product.__table__.update()
        .where(repo.Product.sku == bindparam("b_sku"))
        .values(category=bindparam("category"))
    )
    BATCH = 1000
    with repo.SessionLocal() as s:
        for i in range(0, len(diffs), BATCH):
            s.execute(stmt, diffs[i:i + BATCH])
        s.commit()
    print(f"[apply_category_corrections] updated {len(diffs)} rows")

    # Centroids are means of each category's vectors -- they must be
    # recomputed now that category membership changed, or the live
    # classifier keeps scoring against the OLD (pre-fix) groupings.
    model_version = config.index_fingerprint(catalog_hash=catalog_content_hash(catalog))
    repo.refresh_category_centroids(model_version)
    print("[apply_category_corrections] done.")


if __name__ == "__main__":
    main()
