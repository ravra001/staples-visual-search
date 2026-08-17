"""
Product catalog — pluggable repository.

This module is the ONLY place that knows how products are stored. It exposes a
small repository interface (get_all_products / get_product_by_sku /
get_products_by_category / search_products) backed by one of two providers,
selected via the DATA_BACKEND env var:

  memory  (default)  The in-memory PRODUCTS list below. Zero setup.
  sql                SQLAlchemy against DATABASE_URL (e.g. Cloud SQL Postgres).
                     Same interface, real database. See products_repo_sql.py.

Nothing else in the app changes when you switch backends — main.py just calls
these four functions.
"""
import json
import os

import config

# category -> (base_color RGB, accent_color RGB, shape) used by the image generator
CATEGORY_STYLE = {
    "ink_toner": {"color": (30, 30, 30), "accent": (0, 122, 194), "shape": "cartridge"},
    "cable": {"color": (20, 20, 20), "accent": (255, 176, 0), "shape": "cable"},
    "chair": {"color": (60, 60, 65), "accent": (200, 30, 40), "shape": "chair"},
    "monitor": {"color": (15, 15, 15), "accent": (80, 170, 255), "shape": "monitor"},
    "printer": {"color": (235, 235, 235), "accent": (40, 40, 40), "shape": "printer"},
    "paper": {"color": (255, 255, 255), "accent": (180, 180, 180), "shape": "paper"},
    "cleaning": {"color": (0, 150, 100), "accent": (255, 255, 255), "shape": "bottle"},
    "desk": {"color": (120, 80, 45), "accent": (40, 40, 40), "shape": "desk"},
    "stapler": {"color": (200, 30, 40), "accent": (40, 40, 40), "shape": "stapler"},
}

PRODUCTS = [
    # ---- Ink & Toner ----
    {"sku": "STP-INK-1001", "name": "Staples Remanufactured HP 63XL Black Ink Cartridge, High Yield", "brand": "Staples", "category": "ink_toner", "price": 34.99, "list_price": 44.99, "rating": 4.5, "reviews": 812,
     "description": "High-yield remanufactured cartridge compatible with HP DeskJet and OfficeJet printers. Prints up to 480 pages and is quality-tested for crisp, smudge-free black text."},
    {"sku": "STP-INK-1002", "name": "Staples Remanufactured HP 63XL Tri-Color Ink Cartridge, High Yield", "brand": "Staples", "category": "ink_toner", "price": 38.99, "list_price": 49.99, "rating": 4.4, "reviews": 605,
     "description": "Tri-color high-yield cartridge for vivid photos and documents. A cost-effective remanufactured alternative to the HP original with the same page yield."},
    {"sku": "STP-TON-1003", "name": "Staples Remanufactured Brother TN660 Toner Cartridge, Black, High Yield", "brand": "Staples", "category": "ink_toner", "price": 42.99, "list_price": 54.99, "rating": 4.6, "reviews": 341,
     "description": "High-yield black toner for Brother mono laser printers. Delivers approximately 2,600 pages of sharp text at a fraction of the OEM price."},
    {"sku": "STP-INK-1004", "name": "Canon PG-245XL Black Ink Cartridge, High Yield", "brand": "Canon", "category": "ink_toner", "price": 29.99, "list_price": 36.99, "rating": 4.3, "reviews": 1204,
     "description": "Genuine Canon high-yield black ink for PIXMA all-in-one printers. FINE cartridge technology for reliable everyday document printing."},
    {"sku": "STP-TON-1005", "name": "HP 58A Black Original LaserJet Toner Cartridge", "brand": "HP", "category": "ink_toner", "price": 79.99, "list_price": 99.99, "rating": 4.7, "reviews": 289,
     "description": "Original HP LaserJet toner engineered for consistent, professional results. JetIntelligence technology helps you print more pages and avoid counterfeits."},

    # ---- Cables ----
    {"sku": "STP-CBL-2001", "name": "Staples 6' USB-C to USB-C Charging Cable, Black", "brand": "Staples", "category": "cable", "price": 12.99, "list_price": 17.99, "rating": 4.5, "reviews": 998,
     "description": "6-foot USB-C to USB-C cable supporting fast charging and data sync. Durable braided jacket and reinforced connectors for everyday desk and travel use."},
    {"sku": "STP-CBL-2002", "name": "Staples 6' HDMI Cable, 4K High Speed, Black", "brand": "Staples", "category": "cable", "price": 14.99, "list_price": 19.99, "rating": 4.6, "reviews": 512,
     "description": "High-speed HDMI cable supporting 4K resolution at 60Hz and audio return channel. Ideal for connecting laptops, monitors, and displays."},
    {"sku": "STP-CBL-2003", "name": "Staples 10' Ethernet Cable Cat6, Black", "brand": "Staples", "category": "cable", "price": 9.99, "list_price": 13.99, "rating": 4.4, "reviews": 276,
     "description": "10-foot Cat6 Ethernet cable rated for gigabit speeds. Snagless molded boots protect the clips from breaking during frequent moves."},
    {"sku": "STP-CBL-2004", "name": "Staples 3' Lightning to USB-A Charging Cable, White", "brand": "Staples", "category": "cable", "price": 11.99, "list_price": 15.99, "rating": 4.2, "reviews": 743,
     "description": "MFi-style 3-foot Lightning cable for charging and syncing iPhone and iPad. Compact length keeps nightstands and desks tidy."},

    # ---- Chairs ----
    {"sku": "STP-CHR-3001", "name": "Staples Kelburne Mesh Task Chair, Black", "brand": "Staples", "category": "chair", "price": 129.99, "list_price": 179.99, "rating": 4.3, "reviews": 456,
     "description": "Breathable mesh back with adjustable lumbar support and padded seat. Pneumatic height adjustment and tilt lock keep you comfortable through long workdays."},
    {"sku": "STP-CHR-3002", "name": "Staples Sorina Bonded Leather Manager Chair, Gray", "brand": "Staples", "category": "chair", "price": 189.99, "list_price": 249.99, "rating": 4.5, "reviews": 233,
     "description": "Executive manager chair wrapped in soft bonded leather with generous cushioning. Waterfall seat edge and smooth-rolling casters for the home office."},
    {"sku": "STP-CHR-3003", "name": "Staples Carder Fabric Task Chair, Red", "brand": "Staples", "category": "chair", "price": 99.99, "list_price": 139.99, "rating": 4.1, "reviews": 178,
     "description": "Compact fabric task chair with contoured back and flip-up arms. A colorful, space-saving pick for smaller workspaces."},

    # ---- Monitors ----
    {"sku": "STP-MON-4001", "name": "Dell 24\" FHD IPS LED Monitor, Black", "brand": "Dell", "category": "monitor", "price": 149.99, "list_price": 189.99, "rating": 4.6, "reviews": 892,
     "description": "24-inch Full HD IPS panel with wide viewing angles and thin bezels. HDMI and VGA inputs make it an easy upgrade for any desk setup."},
    {"sku": "STP-MON-4002", "name": "Acer 27\" QHD Curved Gaming Monitor, Black", "brand": "Acer", "category": "monitor", "price": 229.99, "list_price": 299.99, "rating": 4.7, "reviews": 401,
     "description": "27-inch 1440p curved display with a fast refresh rate and AMD FreeSync for tear-free gaming. Immersive curve wraps the action around you."},
    {"sku": "STP-MON-4003", "name": "LG 24\" IPS FHD Monitor with AMD FreeSync, Matte Black", "brand": "LG", "category": "monitor", "price": 89.99, "list_price": 149.99, "rating": 4.5, "reviews": 335,
     "description": "Value 24-inch IPS monitor with 100Hz refresh and FreeSync. Slim three-side borderless design is great for dual-monitor productivity."},

    # ---- Printers ----
    {"sku": "STP-PRT-5001", "name": "HP OfficeJet Pro 9015e Wireless All-in-One Printer, White", "brand": "HP", "category": "printer", "price": 229.99, "list_price": 279.99, "rating": 4.4, "reviews": 621,
     "description": "Wireless all-in-one that prints, scans, copies, and faxes with automatic two-sided printing. Includes months of Instant Ink and smart-tasks automation."},
    {"sku": "STP-PRT-5002", "name": "Brother HL-L2350DW Wireless Laser Printer, Black", "brand": "Brother", "category": "printer", "price": 149.99, "list_price": 189.99, "rating": 4.6, "reviews": 337,
     "description": "Compact monochrome laser printer with wireless and automatic duplex printing. Fast 32 ppm output keeps busy home offices moving."},
    {"sku": "STP-PRT-5003", "name": "Canon PIXMA TR4720 Wireless Inkjet All-in-One Printer", "brand": "Canon", "category": "printer", "price": 69.99, "list_price": 107.99, "rating": 4.2, "reviews": 977,
     "description": "Affordable 4-in-1 inkjet with print, scan, copy, and fax plus a 20-sheet auto document feeder. Wireless and mobile printing built in."},

    # ---- Paper ----
    {"sku": "STP-PPR-6001", "name": "Staples Copy Paper, 8.5\" x 11\", Case of 5000", "brand": "Staples", "category": "paper", "price": 44.99, "list_price": 54.99, "rating": 4.8, "reviews": 2103,
     "description": "Everyday 20 lb multipurpose copy paper, 92 brightness, acid-free. Case of 10 reams (5,000 sheets) runs cleanly in copiers, printers, and fax machines."},
    {"sku": "STP-PPR-6002", "name": "Staples Cardstock, 65 lb, White, 250/Pack", "brand": "Staples", "category": "paper", "price": 12.99, "list_price": 16.99, "rating": 4.5, "reviews": 312,
     "description": "Heavyweight 65 lb white cardstock for flyers, invitations, and crafts. Smooth finish feeds reliably through inkjet and laser printers."},
    {"sku": "STP-PPR-6003", "name": "HammerMill Premium Color Copy Paper, 8.5\" x 11\", 500 Sheets", "brand": "Hammermill", "category": "paper", "price": 13.49, "list_price": 18.99, "rating": 4.7, "reviews": 1580,
     "description": "Ultra-smooth 28 lb, 100-bright paper engineered for vivid color laser output. A single ream for presentations and marketing pieces that pop."},

    # ---- Cleaning ----
    {"sku": "STP-CLN-7001", "name": "Clorox Disinfecting Wipes, 75 Count Canister", "brand": "Clorox", "category": "cleaning", "price": 6.99, "list_price": 8.99, "rating": 4.7, "reviews": 3021,
     "description": "Bleach-free disinfecting wipes that kill 99.9% of viruses and bacteria. Handy 75-count canister for desks, breakrooms, and shared surfaces."},
    {"sku": "STP-CLN-7002", "name": "Purell Advanced Hand Sanitizer, 8 fl oz Pump", "brand": "Purell", "category": "cleaning", "price": 5.49, "list_price": 6.99, "rating": 4.6, "reviews": 1876,
     "description": "70% ethyl alcohol gel sanitizer with skin conditioners. 8 oz tabletop pump for reception desks, checkout counters, and workstations."},
    {"sku": "STP-CLN-7003", "name": "Windex Original Glass Cleaner, 26 fl oz Spray", "brand": "Windex", "category": "cleaning", "price": 4.29, "list_price": 5.49, "rating": 4.8, "reviews": 2447,
     "description": "Streak-free ammonia glass cleaner for windows, monitors, and glass desks. Trigger spray bottle for quick, everyday shine."},

    # ---- Desks ----
    {"sku": "STP-DSK-8001", "name": "Staples Kelburne 60\" Writing Desk, Cherry", "brand": "Staples", "category": "desk", "price": 219.99, "list_price": 279.99, "rating": 4.4, "reviews": 198,
     "description": "Spacious 60-inch writing desk with a warm cherry laminate finish and a durable steel frame. Cable grommets keep your workspace organized."},
    {"sku": "STP-DSK-8002", "name": "Staples Carver 48\" Height-Adjustable Standing Desk, Black", "brand": "Staples", "category": "desk", "price": 299.99, "list_price": 379.99, "rating": 4.5, "reviews": 267,
     "description": "Electric sit-stand desk with programmable height presets and a sturdy dual-motor base. Switch between sitting and standing throughout the day."},
    {"sku": "STP-DSK-8003", "name": "Union & Scale Essentials L-Shaped Desk, Gray Oak", "brand": "Union & Scale", "category": "desk", "price": 189.99, "list_price": 259.99, "rating": 4.3, "reviews": 142,
     "description": "Corner L-shaped desk that maximizes a room's footprint. Reversible design fits left or right layouts with plenty of surface for dual monitors."},

    # ---- Staplers / office ----
    {"sku": "STP-OFC-9001", "name": "Staples Heavy-Duty Stapler, 40-Sheet Capacity, Red", "brand": "Staples", "category": "stapler", "price": 16.99, "list_price": 21.99, "rating": 4.6, "reviews": 542,
     "description": "All-metal heavy-duty stapler that drives through up to 40 sheets. Full-strip capacity and a non-slip base for high-volume desks."},
    {"sku": "STP-OFC-9002", "name": "Swingline Standard Stapler, Black", "brand": "Swingline", "category": "stapler", "price": 9.99, "list_price": 12.99, "rating": 4.5, "reviews": 987,
     "description": "Classic 20-sheet desktop stapler with a jam-free mechanism. A dependable everyday staple for home and office."},
    {"sku": "STP-OFC-9003", "name": "Staples One-Touch Desktop Stapler, 25-Sheet, Black/Silver", "brand": "Staples", "category": "stapler", "price": 13.49, "list_price": 17.99, "rating": 4.4, "reviews": 421,
     "description": "Reduced-effort stapler that needs half the force to fasten up to 25 sheets. Ergonomic design is easy on the hand during long sessions."},
]

# --------------------------------------------------------------------------
# Optional external catalog file (e.g. the 10k ABO dataset from ingest_abo.py).
# Set CATALOG_FILE=data/catalog_abo.json to replace the built-in demo catalog.
# --------------------------------------------------------------------------

def load_catalog_file(catalog_file):
    """Parse a catalog JSON file, dropping private ingest-only fields (keys
    starting with "_"). Factored out so init_and_seed (products_repo_sql.py)
    can load the real catalog explicitly under DATA_BACKEND=sql, where
    PRODUCTS below is deliberately NOT the full file (see comment there)."""
    path = catalog_file if os.path.isabs(catalog_file) else os.path.join(os.path.dirname(__file__), catalog_file)
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in loaded]


_CATALOG_FILE = config.CATALOG_FILE
DATA_BACKEND = config.DATA_BACKEND

# Under DATA_BACKEND=sql, nothing at runtime reads PRODUCTS or _BY_SKU — every
# request goes through the dispatched get_all_products()/etc below, which are
# rebound entirely to products_repo_sql. The only consumer of the full catalog
# under sql is the one-off init_and_seed() management command, which loads it
# explicitly via load_catalog_file() rather than depending on this module-level
# copy. So skip parsing (and holding in RAM for the rest of the process's
# life) a ~6MB catalog file a live sql-backed instance will never look at.
if _CATALOG_FILE and DATA_BACKEND != "sql":
    PRODUCTS = load_catalog_file(_CATALOG_FILE)
    print(f"[catalog] loaded {len(PRODUCTS)} products from {_CATALOG_FILE}")


# --------------------------------------------------------------------------
# In-memory backend
# --------------------------------------------------------------------------

# O(1) sku lookup, built once (the catalog is static in memory mode). Skipped
# under sql for the same reason as above — PRODUCTS is the small built-in demo
# list there, not the real catalog, and nothing reads _BY_SKU in that mode.
_BY_SKU = {p["sku"]: p for p in PRODUCTS} if DATA_BACKEND != "sql" else {}

def _mem_all():
    return PRODUCTS

def _mem_by_sku(sku):
    return _BY_SKU.get(sku)

def _mem_by_skus(skus):
    """Batch lookup — returns {sku: product} for the found skus (single pass)."""
    return {s: _BY_SKU[s] for s in skus if s in _BY_SKU}

def _mem_by_category(category, order_by_price=False):
    items = [p for p in PRODUCTS if p["category"] == category]
    if order_by_price:
        items = sorted(items, key=lambda p: p.get("price") or float("inf"))
    return items

def _mem_search(query):
    """AND-across-terms substring match, ordered by relevance (how many
    terms matched in the NAME specifically -- a stronger signal than a term
    only appearing in the description) rather than left in arbitrary
    catalog order. See products_repo_sql.search_products_ranked_skus for
    the sql-backend equivalent; both feed main.py's hybrid text_search."""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = q.split()
    scored = []
    for p in PRODUCTS:
        haystack = f"{p['name']} {p.get('brand', '')} {p['category']} {p.get('description', '')}".lower()
        if all(t in haystack for t in terms):
            name_l = p["name"].lower()
            relevance = sum(1 for t in terms if t in name_l)
            scored.append((relevance, p))
    scored.sort(key=lambda pair: -pair[0])
    return [p for _relevance, p in scored]


# --------------------------------------------------------------------------
# Backend dispatch
# --------------------------------------------------------------------------

if DATA_BACKEND == "sql":
    # Lazy import so the default path never needs SQLAlchemy installed.
    from products_repo_sql import (
        get_all_products,
        get_product_by_sku,
        get_products_by_skus,
        get_products_by_category,
        search_products,
    )
else:
    get_all_products = _mem_all
    get_product_by_sku = _mem_by_sku
    get_products_by_skus = _mem_by_skus
    get_products_by_category = _mem_by_category
    search_products = _mem_search


def catalog_content_hash(products=None):
    """Short hash of every product's sku+name+brand+description, so an index
    cache can detect when catalog TEXT changed (not just the model) — a fused
    vector is 70% a function of this text, so an edit here silently invalidates
    the cached vector even though embedding.backend/model didn't change.

    products: explicit list to hash. Defaults to get_all_products() — correct
    for the in-memory backend (that call IS the catalog). Under
    DATA_BACKEND=sql, get_all_products() queries the live Postgres table
    instead, which during seeding is exactly the wrong thing to hash (either
    empty on a first seed, or the OLD content on a re-seed) — so
    init_and_seed() always passes PRODUCTS (the just-loaded catalog file)
    explicitly here rather than relying on the default."""
    import hashlib
    h = hashlib.sha1()
    for p in sorted(products if products is not None else get_all_products(), key=lambda p: p["sku"]):
        h.update(f"{p['sku']}|{p.get('name', '')}|{p.get('brand', '')}|{p.get('description', '')[:300]}".encode("utf-8"))
    return h.hexdigest()[:12]
