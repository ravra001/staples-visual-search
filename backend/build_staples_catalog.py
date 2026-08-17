"""
Filters a McAuley-Lab Amazon-Reviews-2023 metadata dump (JSONL, one product
per line) into a Staples-like candidate catalog, grouped into office-supply
categories by keyword-matching the Amazon breadcrumb + title.

This is a CANDIDATE catalog for review, not a drop-in replacement: image
URLs point at Amazon's own CDN (m.media-amazon.com) rather than our own
static/images or GCS+CDN origin, and nothing here touches the live products
table. Rehosting the images and running ingest_abo.py's embedding step are
separate follow-up steps once the catalog itself is approved.

Usage:
  python build_staples_catalog.py <path-to-meta_Office_Products.jsonl> \
      --out-prefix staples_candidate --limit-per-category 1200
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, OrderedDict

PROFILES = {
    "office": OrderedDict([
        ("ink_toner", ["ink cartridge", "toner cartridge", "inkjet cartridge", "laser toner", "ink refill", "toner refill"]),
        ("printers_scanners", ["printer", "scanner", "fax machine", "photocopier", " copier"]),
        ("office_electronics", ["calculator", "paper shredder", "laminator", "label maker", "label printer", "time clock", "binding machine", "projector"]),
        ("office_furniture", ["office chair", "desk chair", "file cabinet", "filing cabinet", "bookcase", "office desk", "standing desk", "desk lamp", "drafting table"]),
        ("filing_storage", ["file folder", "hanging folder", "storage box", "binder", "expanding file", "magazine file", "index card", "file box", "file organizer"]),
        ("mailing_shipping", ["shipping label", "mailer", "packing tape", "bubble mailer", "postage", "mailing envelope", "shipping box"]),
        ("presentation_supplies", ["whiteboard", "dry erase", "easel", "flip chart", "bulletin board", "presentation board", "cork board"]),
        ("writing_supplies", ["ballpoint pen", "gel pen", "pencil", "marker", "highlighter", "correction tape", "correction fluid", "permanent marker"]),
        ("office_supplies", ["paper clip", "binder clip", "rubber band", "stapler", "staples", "tape dispenser", "scissors", "glue stick", "sticky note", "post-it", "envelope", "notebook", "notepad", "rubber stamp", "ruler", "paper"]),
    ]),
    "electronics": OrderedDict([
        ("computers_tablets", ["laptop computer", "gaming laptop", "business laptop", "student laptop", "ultrabook", "chromebook", "notebook computer", "desktop computer", "desktop pc", "all-in-one computer", "mini pc", "android tablet", "windows tablet", "2-in-1 laptop"]),
        ("monitors", ["computer monitor", "led monitor", "curved monitor", "ultrawide monitor"]),
        ("computer_accessories", ["wireless mouse", "computer mouse", "mechanical keyboard", "computer keyboard", "webcam", "usb hub", "docking station", "external hard drive", "external ssd", "usb flash drive", "laptop stand", "laptop bag", "laptop sleeve", "laptop cooling pad", "monitor stand", "monitor arm"]),
        ("networking", ["wifi router", "wireless router", "network switch", "wifi extender", "cable modem", "powerline adapter", "ethernet cable"]),
        ("computer_components", ["graphics card", "solid state drive", "internal hard drive", "desktop memory", "motherboard", "power supply unit", "cpu cooler", "pc case"]),
        ("cables_chargers", ["usb-c cable", "hdmi cable", "laptop charger", "portable power bank", "surge protector", "usb wall charger"]),
    ]),
}

EXCLUDE_KEYWORDS = ["cell phone", "iphone", "smartphone", "samsung galaxy", "camera", "camcorder", "car stereo", "car audio", "dash cam", "home theater", "television", " tv ", "gaming console", "playstation", "xbox", "nintendo", "drone", "smartwatch", "headphone", "earbud", "bluetooth speaker"]

# Accessory-indicator words: if present, a title never counts as a device
# itself (computers_tablets / monitors) even if it also contains a device
# keyword via breadcrumb bleed (e.g. "iPad Case" living under a "Tablet
# Accessories" category node matches "tablet" in the breadcrumb otherwise).
DEVICE_GUARD_CATEGORIES = {"computers_tablets", "monitors"}
ACCESSORY_GUARD_WORDS = ["case", "cover", "skin", "sticker", "decal", "screen protector", "sleeve", "stand for", "mount for", "bag for", "charger for", "cable for", "keyboard for", "adapter for", "replacement"]


def classify(title, categories, keywords_by_cat, excludes=None):
    text = (" ".join(categories) + " " + title).lower()
    if excludes and any(kw in text for kw in excludes):
        return None
    is_accessory = any(kw in text for kw in ACCESSORY_GUARD_WORDS)
    for cat, keywords in keywords_by_cat.items():
        if is_accessory and cat in DEVICE_GUARD_CATEGORIES:
            continue
        for kw in keywords:
            if kw in text:
                return cat
    return None


def best_image(images):
    for im in images or []:
        if im.get("variant") == "MAIN" and (im.get("hi_res") or im.get("large")):
            return im.get("hi_res") or im.get("large"), im.get("thumb") or im.get("large")
    for im in images or []:
        if im.get("hi_res") or im.get("large"):
            return im.get("hi_res") or im.get("large"), im.get("thumb") or im.get("large")
    return None, None


def clean_text(parts, max_len):
    text = " ".join(p.strip() for p in parts if p and p.strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("meta_path")
    ap.add_argument("--profile", choices=list(PROFILES), default="office")
    ap.add_argument("--limit-per-category", type=int, default=1200)
    ap.add_argument("--sku-prefix", default="STP")
    ap.add_argument("--out-prefix", default="staples_candidate")
    args = ap.parse_args()

    keywords_by_cat = PROFILES[args.profile]
    excludes = EXCLUDE_KEYWORDS if args.profile == "electronics" else None

    counts = Counter()
    seen_asins = set()
    products = []
    cap_total = args.limit_per_category * len(keywords_by_cat)

    with open(args.meta_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            asin = rec.get("parent_asin")
            title = (rec.get("title") or "").strip()
            price = rec.get("price")
            if not asin or asin in seen_asins or not title or price in (None, ""):
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0 or price > 5000:
                continue

            hi_res, thumb = best_image(rec.get("images"))
            if not hi_res:
                continue

            categories = rec.get("categories") or []
            cat = classify(title, categories, keywords_by_cat, excludes)
            if not cat or counts[cat] >= args.limit_per_category:
                continue

            desc_parts = rec.get("description") or rec.get("features") or []
            description = clean_text(desc_parts, 600) or title

            brand = (rec.get("store") or "").strip()[:80]
            rating = rec.get("average_rating") or 0.0
            reviews = rec.get("rating_number") or 0
            list_price = round(price * 1.18, 2)

            products.append({
                "sku": f"{args.sku_prefix}-{asin}",
                "name": title[:255],
                "brand": brand,
                "category": cat,
                "price": round(price, 2),
                "list_price": list_price,
                "rating": round(float(rating), 1),
                "reviews": int(reviews),
                "description": description,
                "image_url": hi_res,
                "thumbnail_url": thumb,
            })
            seen_asins.add(asin)
            counts[cat] += 1

            if line_no % 200000 == 0:
                print(f"...scanned {line_no} lines, collected {len(products)}", file=sys.stderr)

            if sum(counts.values()) >= cap_total:
                break

    print("Category counts:", dict(counts), file=sys.stderr)
    print("Total products:", len(products), file=sys.stderr)

    with open(f"{args.out_prefix}.json", "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False)

    fieldnames = ["sku", "name", "brand", "category", "price", "list_price", "rating", "reviews", "description", "image_url", "thumbnail_url"]
    with open(f"{args.out_prefix}.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in products:
            writer.writerow(p)


if __name__ == "__main__":
    main()
