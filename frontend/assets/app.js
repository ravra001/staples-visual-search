// Staples visual search demo — vanilla JS, no build step needed.

const API_BASE = ""; // same-origin, FastAPI serves both API and frontend

// Escape catalog text before interpolating into innerHTML. Product names/
// descriptions come from the ABO dataset (third-party scraped text) — safe
// today since it's static/trusted at build time, but this is the seam where
// that assumption would break (e.g. once product data comes from a live DB
// via the sql backend), so escape defensively rather than trust the source.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- Persistent cart (localStorage) ----------
// Stores actual line items ({sku: qty}), not just a running count, so a
// dedicated cart page can list/edit/remove what's actually in it.
const CART_KEY = "vsCart";

function getCart() {
  try {
    const raw = JSON.parse(localStorage.getItem(CART_KEY) || "{}");
    return (raw && typeof raw === "object" && !Array.isArray(raw)) ? raw : {};
  } catch (e) {
    return {};
  }
}
function setCart(cart) {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  const count = Object.values(cart).reduce((a, b) => a + b, 0);
  document.querySelectorAll("#cart-count").forEach(el => { el.textContent = count; });
}
function getCartCount() {
  return Object.values(getCart()).reduce((a, b) => a + b, 0);
}
function addToCart(sku, qty = 1) {
  const cart = getCart();
  cart[sku] = (cart[sku] || 0) + qty;
  setCart(cart);
}
function setCartItemQty(sku, qty) {
  const cart = getCart();
  if (qty <= 0) delete cart[sku]; else cart[sku] = qty;
  setCart(cart);
}
function removeFromCart(sku) {
  setCartItemQty(sku, 0);
}

// ---------- Shared site chrome (header + footer) ----------
// Rendered from JS so all pages share one source of truth (no server-side
// templating in this demo). Pages just drop <div id="site-header"></div> and
// <div id="site-footer"></div> and call renderSiteChrome() before wiring.

const PROMO_MESSAGES = [
  "Free shipping on orders $45+ — no membership required",
  "Up to $125 off custom print orders",
  "Snap a photo to shop — try Visual Search →",
];

// Category display labels. Categories themselves come live from /api/categories
// (so the nav matches whatever catalog is loaded — the 30-item demo or the 10k
// ABO set); anything without an explicit label is prettified automatically.
const CATEGORY_LABELS = {
  // demo office catalog
  ink_toner: "Ink & Toner", cable: "Computers & Accessories", chair: "Furniture",
  monitor: "Monitors", printer: "Printers & Scanners", paper: "Paper",
  cleaning: "Cleaning Supplies", desk: "Desks", stapler: "Office Supplies",
  // 10k ABO catalog
  chairs: "Chairs", sofas: "Sofas", tables: "Tables", desks: "Desks",
  storage: "Storage", furniture: "Furniture", home_decor: "Home & Décor",
  wall_art: "Wall Art", rugs: "Rugs", lighting: "Lighting",
  office_supplies: "Office Supplies", kitchen: "Kitchen & Breakroom",
};

function prettifyCategory(cat) {
  return CATEGORY_LABELS[cat] || cat.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// Fetch the live category list once and reuse the promise across callers.
let _categoriesPromise = null;
function getCategories() {
  if (!_categoriesPromise) {
    _categoriesPromise = fetch(`${API_BASE}/api/categories`)
      .then(r => r.json()).then(d => d.categories || [])
      .catch(() => []);
  }
  return _categoriesPromise;
}

const ICONS = {
  camera: `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 8a2 2 0 0 1 2-2h1.5l1-1.5h7l1 1.5H18a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z"/><circle cx="12" cy="12.5" r="3.5"/></svg>`,
  search: `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
  pin: `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 21s-6-5.3-6-10a6 6 0 0 1 12 0c0 4.7-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>`,
  user: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="8" r="3.5"/><path d="M4.5 20c1.5-4 4.5-6 7.5-6s6 2 7.5 6"/></svg>`,
  orders: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 4h11l1 4H5"/><path d="M5 8v10a1 1 0 0 0 1 1h11a1 1 0 0 0 1-1V8"/><line x1="9" y1="12" x2="14" y2="12"/></svg>`,
  cart: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 4h2l2.4 12.4a2 2 0 0 0 2 1.6h7.2a2 2 0 0 0 2-1.6L20 8H6"/><circle cx="9.5" cy="20.5" r="1.2"/><circle cx="17" cy="20.5" r="1.2"/></svg>`,
  chevron: `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>`,
};

const FOOTER_COLUMNS = [
  { title: "Customer Service", links: ["Help Center", "Track Order", "Returns", "Shipping", "Contact Us", "Store Locator", "Warranty & Protection"] },
  { title: "Company Information", links: ["About Staples", "Careers", "Staples in the News", "Supplier Diversity", "Investor Relations", "Accessibility Commitment", "Affiliate Program"] },
  { title: "Staples for Business", links: ["Breakroom Solutions", "Facility Solutions", "Furniture Solutions", "Print Solutions", "Tech Solutions", "Contact Staples Business"] },
  { title: "Services", links: ["Print & Marketing", "Copies & Documents", "Recycling", "Shipping Services", "Tech Services & Support", "Passport & TSA Services", "Promotional Products"] },
  { title: "Staples Programs", links: ["Easy Rewards", "Weekly Ad", "Coupons & Offers", "Gift Cards", "Staples Credit Card", "Staples Connect App"] },
];

function renderSiteHeader() {
  const host = document.getElementById("site-header");
  if (!host) return;
  host.innerHTML = `
  <header class="site-header">
    <div class="promo-bar"><div class="promo-inner">
      <span id="promo-text">${PROMO_MESSAGES[0]}</span>
      <span id="backend-badge" class="backend-badge" hidden></span>
    </div></div>

    <div class="main-nav">
      <div class="main-nav-inner">
        <div class="menu-wrap">
          <button class="hamburger icon-btn" id="menu-btn" aria-label="Menu" aria-haspopup="true" aria-expanded="false">
            <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
          </button>
          <div class="menu-dropdown" id="menu-dropdown" hidden>
            <a class="menu-feature" href="/pitch.html">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
              <span><strong>Project Overview</strong><small>Business value &amp; what's built, in one page</small></span>
            </a>
            <a class="menu-feature" href="/architecture.html">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 21h18"/><path d="M5 21V9l7-5 7 5v12"/><path d="M9 21v-6h6v6"/></svg>
              <span><strong>Architecture Overview</strong><small>On-prem &amp; GCP architecture, explained visually</small></span>
            </a>
            <a class="menu-feature" href="/how-it-works.html">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"/></svg>
              <span><strong>How It Works</strong><small>The search pipeline, step by step</small></span>
            </a>
            <div class="menu-divider"></div>
            <p class="menu-label">Shop by category</p>
            <div id="menu-categories"></div>
            <div class="menu-divider"></div>
            <a class="menu-item" href="/">Home</a>
            <a class="menu-item" href="/category.html">All products</a>
          </div>
        </div>

        <a class="logo" href="/">Staples<span class="logo-dot">.</span></a>

        <button class="location-chip" title="Set your store">
          ${ICONS.pin}
          <span class="location-text"><small>Pickup in store</small><strong>Set your store</strong></span>
        </button>

        <div class="search-wrap">
          <form id="text-search-form" autocomplete="off">
            <input id="text-search-input" type="text" placeholder="What can we help you find today?" />
            <button type="button" id="camera-btn" class="icon-btn cam-btn" title="Search by image" aria-label="Search by image">${ICONS.camera}</button>
            <input id="visual-search-input" type="file" accept="image/*" hidden />
            <button type="submit" class="icon-btn search-submit" aria-label="Search">${ICONS.search}</button>
          </form>
        </div>

        <div class="account-icons">
          <a class="acct-link" href="#" title="Sign In">${ICONS.user}<span>Sign In</span></a>
          <a class="acct-link" href="#" title="Orders">${ICONS.orders}<span>Orders</span></a>
          <a class="acct-link cart-btn" href="/cart.html" title="Cart" aria-label="Cart">
            ${ICONS.cart}<span>Cart</span>
            <span id="cart-count" class="cart-badge">${getCartCount()}</span>
          </a>
        </div>
      </div>

      <nav class="primary-links">
        <div class="primary-links-inner">
          <a href="#" class="nav-shop">Shop ${ICONS.chevron}</a>
          <a href="#">Deals</a>
          <a href="#">Print &amp; Marketing</a>
          <a href="#">For Business</a>
          <span class="nav-divider"></span>
          <div class="category-strip" id="category-strip"></div>
        </div>
      </nav>
    </div>
  </header>`;
  wireHeaderInteractions();
}

function wireHeaderInteractions() {
  // Rotating promo message
  const promo = document.getElementById("promo-text");
  if (promo && PROMO_MESSAGES.length > 1) {
    let i = 0;
    setInterval(() => {
      i = (i + 1) % PROMO_MESSAGES.length;
      promo.style.opacity = "0";
      setTimeout(() => { promo.textContent = PROMO_MESSAGES[i]; promo.style.opacity = "1"; }, 250);
    }, 4000);
  }
  // Header menu dropdown (Architecture + category nav)
  const menuBtn = document.getElementById("menu-btn");
  const dropdown = document.getElementById("menu-dropdown");
  if (menuBtn && dropdown) {
    const setOpen = (open) => {
      dropdown.hidden = !open;
      menuBtn.setAttribute("aria-expanded", String(open));
    };
    menuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(dropdown.hidden);
    });
    document.addEventListener("click", (e) => {
      if (!dropdown.hidden && !dropdown.contains(e.target) && e.target !== menuBtn) setOpen(false);
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
  }
  // Live backend badge (demo transparency) — reflects which embedding/data
  // backends the server is actually running, from GET /api/config.
  renderBackendBadge();
  // Category nav (strip + dropdown) from the live catalog.
  renderCategoryNav();
}

async function renderCategoryNav() {
  const cats = await getCategories();
  const strip = document.getElementById("category-strip");
  const menu = document.getElementById("menu-categories");
  if (strip) {
    // keep the top nav strip readable — cap it, the rest live in the menu
    strip.innerHTML = cats.slice(0, 9).map(c =>
      `<a href="/category.html?category=${encodeURIComponent(c)}">${prettifyCategory(c)}</a>`).join("");
  }
  if (menu) {
    menu.innerHTML = cats.map(c =>
      `<a class="menu-item" href="/category.html?category=${encodeURIComponent(c)}">${prettifyCategory(c)}</a>`).join("");
  }
}

const BACKEND_LABELS = {
  heuristic: "Heuristic",
  clip: "CLIP (local)",
  vertex: "Vertex AI",
  memory: "In-memory",
  sql: "Cloud SQL",
};

async function renderBackendBadge() {
  const badge = document.getElementById("backend-badge");
  if (!badge) return;
  try {
    const res = await fetch(`${API_BASE}/api/config`);
    if (!res.ok) return;
    const cfg = await res.json();
    const emb = BACKEND_LABELS[cfg.embedding_backend] || cfg.embedding_backend;
    const data = BACKEND_LABELS[cfg.data_backend] || cfg.data_backend;
    const live = cfg.embedding_backend !== "heuristic"; // green dot once a real model is on
    badge.innerHTML = `<span class="badge-dot ${live ? "live" : ""}"></span>Search: ${emb} · Data: ${data}`;
    badge.title = `Embedding backend: ${cfg.embedding_backend} · Data backend: ${cfg.data_backend}`;
    badge.hidden = false;
  } catch (e) {
    /* badge is optional — stay hidden on failure */
  }
}

function renderSiteFooter() {
  const host = document.getElementById("site-footer");
  if (!host) return;
  const cols = FOOTER_COLUMNS.map(col => `
    <div class="footer-col">
      <h4>${col.title}</h4>
      <ul>${col.links.map(l => `<li><a href="#">${l}</a></li>`).join("")}</ul>
    </div>`).join("");
  const year = new Date().getFullYear();
  host.innerHTML = `
  <footer class="site-footer">
    <div class="footer-cols">${cols}</div>
    <div class="footer-social">
      <span>Connect with us</span>
      <div class="social-row" aria-hidden="true">
        <span class="social-dot">f</span><span class="social-dot">X</span>
        <span class="social-dot">in</span><span class="social-dot">▶</span><span class="social-dot">◎</span>
      </div>
    </div>
    <div class="footer-bar">
      <p>© 1998–${year} Staples, Inc. — <strong>Demo prototype, not affiliated with Staples, Inc.</strong> Product photography from the open Amazon Berkeley Objects dataset (CC BY-NC 4.0) — demo use only.</p>
      <p class="footer-legal"><a href="#">Site Map</a> · <a href="#">Privacy Notice</a> · <a href="#">Terms &amp; Conditions</a> · <a href="#">California Notice</a></p>
    </div>
  </footer>`;
}

// Convenience: render both, then return so callers can wire page behavior.
function renderSiteChrome() {
  renderSiteHeader();
  renderSiteFooter();
  wireGlobalImageDrop();
}

// A handful of representative SKUs spanning categories, so the landing page
// shows a diverse "sample products" set at a glance.
const SAMPLE_SKUS = [
  "STP-CHR-3001", // task chair
  "STP-MON-4002", // curved monitor
  "STP-PRT-5001", // all-in-one printer
  "STP-INK-1001", // ink cartridge
  "STP-CBL-2001", // USB-C cable
  "STP-DSK-8002", // standing desk
  "STP-PPR-6001", // copy paper
  "STP-OFC-9001", // stapler
];

// Renders a fixed sample of products (by SKU) into a grid. Fetches the full
// catalog once, then filters — preserving SAMPLE_SKUS order.
async function renderSampleProducts(elId, skus = SAMPLE_SKUS) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading…</div>`;
  try {
    const res = await fetch(`${API_BASE}/api/products`);
    if (!res.ok) throw new Error("Could not load products");
    const data = await res.json();
    const bySku = Object.fromEntries(data.items.map(p => [p.sku, p]));
    let picks = skus.map(s => bySku[s]).filter(Boolean);
    // Fallback for catalogs that don't contain the demo SKUs (e.g. the 10k set):
    // show a spread of the first available products instead.
    if (picks.length < 4) picks = data.items.slice(0, 8);
    el.innerHTML = picks.map(p => productCardHTML(p)).join("");
    wireProductCardInteractions(el);
  } catch (e) {
    el.innerHTML = `<div class="state-msg">${e.message}. Please refresh.</div>`;
  }
}

// Homepage "Shop by Category" tiles — built from the live category list so
// labels/links always match the loaded catalog.
async function renderCategoryGrid(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  const cats = await getCategories();
  el.innerHTML = cats.map(c => `
    <a class="cat-tile" href="/category.html?category=${encodeURIComponent(c)}"><span>${prettifyCategory(c)}</span></a>
  `).join("");
}

function starString(rating) {
  const full = Math.round(rating);
  return "★".repeat(full) + "☆".repeat(5 - full);
}

function productCardHTML(p, opts = {}) {
  const matchBadge = p.match_score !== undefined
    ? `<div class="match-badge">${p.match_score}% match</div>`
    : "";
  const href = `/product.html?sku=${encodeURIComponent(p.sku)}`;
  return `
    <div class="product-card" data-sku="${p.sku}">
      ${matchBadge}
      <div class="product-media">
        <a href="${href}"><img src="${esc(p.image_url)}" alt="${esc(p.name)}" loading="lazy" /></a>
        <a class="find-similar-link" href="/visual-search.html?sku=${encodeURIComponent(p.sku)}" title="Find visually similar products">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
          Find similar
        </a>
      </div>
      <a class="product-name" href="${href}">${esc(p.name)}</a>
      <div class="stars">${starString(p.rating)} <span class="count">(${p.reviews})</span></div>
      <div class="price-row">
        <span class="price">$${p.price.toFixed(2)}</span>
        <span class="list-price">$${p.list_price.toFixed(2)}</span>
      </div>
      <div class="savings">You save ${p.savings_pct}%</div>
      <div class="qty-row">
        <div class="qty-stepper">
          <button class="qty-dec">−</button>
          <span class="qty-val">1</span>
          <button class="qty-inc">+</button>
        </div>
        <button class="add-btn">Add</button>
      </div>
    </div>
  `;
}

function wireProductCardInteractions(container) {
  container.querySelectorAll(".product-card").forEach(card => {
    if (card.dataset.wired) return;   // skip already-wired cards (e.g. "Load more" appends)
    card.dataset.wired = "1";
    const qtyVal = card.querySelector(".qty-val");
    let qty = 1;
    card.querySelector(".qty-inc").addEventListener("click", () => {
      qty++; qtyVal.textContent = qty;
    });
    card.querySelector(".qty-dec").addEventListener("click", () => {
      if (qty > 1) { qty--; qtyVal.textContent = qty; }
    });
    card.querySelector(".add-btn").addEventListener("click", (e) => {
      addToCart(card.dataset.sku, qty);
      e.target.textContent = "Added ✓";
      e.target.classList.add("added");
      setTimeout(() => { e.target.textContent = "Add"; e.target.classList.remove("added"); }, 1200);
    });
  });
}

async function renderProductRail(elId, { category, limit = 12, offset = 0 } = {}) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading…</div>`;
  const q = (cat, off) => `${API_BASE}/api/products?${cat ? `category=${encodeURIComponent(cat)}&` : ""}limit=${limit}&offset=${off}`;
  try {
    let res = await fetch(q(category, offset));
    if (!res.ok) throw new Error("Could not load products");
    let data = await res.json();
    // Fall back to general products if the given category isn't in this catalog
    // (e.g. "chair" vs "chairs" across the demo and 10k catalogs).
    if (category && data.count === 0) { res = await fetch(q(null, offset)); data = await res.json(); }
    el.innerHTML = data.items.map(p => productCardHTML(p)).join("");
    wireProductCardInteractions(el);
  } catch (e) {
    el.innerHTML = `<div class="state-msg">${e.message}. Please refresh.</div>`;
  }
}

// Visual-similarity rail for the PDP — queries FROM an existing product's own
// vector (see /api/products/{sku}/similar). maxPrice, if given, filters to
// strictly-cheaper visually-similar items ("similar for less"); the whole
// section is removed (not left empty) if nothing qualifies.
async function renderSimilarRail(elId, sku, { maxPrice, limit = 12 } = {}) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading…</div>`;
  try {
    const topK = maxPrice != null ? 40 : limit;   // fetch deeper when filtering, since some won't qualify
    const res = await fetch(`${API_BASE}/api/products/${encodeURIComponent(sku)}/similar?top_k=${topK}`);
    if (!res.ok) throw new Error("Could not load similar products");
    const data = await res.json();
    let items = data.items || [];
    if (maxPrice != null) items = items.filter(p => p.price < maxPrice);
    items = items.slice(0, limit);
    if (!items.length) { el.closest("section")?.remove(); return; }
    el.innerHTML = items.map(p => productCardHTML(p)).join("");
    wireProductCardInteractions(el);
  } catch (e) {
    el.closest("section")?.remove();
  }
}

// Drives the listing page for either a category OR a text-search query, with
// server-side pagination + a "Load more" button (never fetches the whole catalog).
const LISTING_PAGE_SIZE = 48;
let _listing = null;   // { base, offset, total }

async function renderListingPage({ category, query } = {}) {
  const grid = document.getElementById("listing-grid");
  const title = document.getElementById("listing-title");
  const countEl = document.getElementById("listing-count");

  if (query) {
    title.textContent = `Results for “${query}”`;
    _listing = { base: `${API_BASE}/api/search?q=${encodeURIComponent(query)}`, offset: 0, total: null, query };
  } else {
    title.textContent = category ? category.replace(/_/g, " ") : "All Products";
    const b = category ? `${API_BASE}/api/products?category=${encodeURIComponent(category)}` : `${API_BASE}/api/products?`;
    _listing = { base: b, offset: 0, total: null };
  }
  grid.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading products…</div>`;
  _removeLoadMore();
  await _loadListingPage(true);

  async function _loadListingPage(reset) {
    try {
      const res = await fetch(`${_listing.base}&limit=${LISTING_PAGE_SIZE}&offset=${_listing.offset}`);
      if (!res.ok) throw new Error("Could not load products");
      const data = await res.json();
      _listing.total = data.count;
      countEl.textContent = `${data.count} result${data.count === 1 ? "" : "s"}`;
      if (reset && data.count === 0) {
        grid.innerHTML = `<div class="state-msg">No products matched${_listing.query ? ` “${esc(_listing.query)}”` : ""}. Try a different search or browse a category.</div>`;
        return;
      }
      const html = data.items.map(p => productCardHTML(p)).join("");
      if (reset) grid.innerHTML = html; else grid.insertAdjacentHTML("beforeend", html);
      wireProductCardInteractions(grid);   // guarded — only wires new cards
      _listing.offset += data.items.length;
      _renderLoadMore(_listing.offset < _listing.total, _loadListingPage);
    } catch (e) {
      if (reset) { countEl.textContent = ""; grid.innerHTML = `<div class="state-msg">${e.message}. Please try again.</div>`; }
    }
  }
}

function _removeLoadMore() {
  document.getElementById("load-more-wrap")?.remove();
}
function _renderLoadMore(hasMore, onClick) {
  _removeLoadMore();
  if (!hasMore) return;
  const grid = document.getElementById("listing-grid");
  const wrap = document.createElement("div");
  wrap.id = "load-more-wrap";
  wrap.innerHTML = `<button class="load-more-btn">Load more</button>`;
  grid.after(wrap);
  wrap.querySelector(".load-more-btn").addEventListener("click", (e) => {
    e.target.textContent = "Loading…";
    onClick(false);
  });
}

// Clickable "try one of these" thumbnails — real catalog photos known to
// return strong matches, so a judge/visitor with no photo on hand can still
// see the full search flow in one click. Reuses the same startVisualSearch
// pipeline as a real upload (fetch the image, wrap it as a File, go).
const HERO_SAMPLE_SKUS = [
  "ABO-B08FHH4RY4", // chair
  "ABO-B07F2X89KT", // sofa
  "ABO-B07MBFDQHP", // table lamp
  "ABO-B07HSN877K", // rug
  "ABO-B073P19TDR", // wall art
  "ABO-B07HSKW3D6", // storage
];

function renderHeroSamples(mountId) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  mount.innerHTML = HERO_SAMPLE_SKUS.map(sku => `
    <button class="hero-sample-thumb" data-sku="${sku}" title="Search with this photo">
      <img src="/images/products/${sku}.jpg" alt="" loading="lazy" />
    </button>`).join("");
  mount.querySelectorAll(".hero-sample-thumb").forEach(btn => {
    btn.addEventListener("click", async () => {
      const sku = btn.dataset.sku;
      const res = await fetch(`/images/products/${sku}.jpg`);
      const blob = await res.blob();
      const file = new File([blob], `${sku}.jpg`, { type: blob.type || "image/jpeg" });
      startVisualSearch(file);
    });
  });
}

// Sample photos for "Shop the Room" — a real multi-item room photo and a
// synthetic ground-truth composite (four distinct catalog products tiled
// into one image, used during development to verify the tile-sweep found
// all four correctly) — both known to produce a real multi-item result.
const ROOM_SAMPLE_IMAGES = [
  { file: "room-1.jpg", label: "Living room photo" },
  { file: "room-2.jpg", label: "Furniture grid" },
];

function renderRoomSamples(mountId) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  mount.innerHTML = ROOM_SAMPLE_IMAGES.map(({ file, label }) => `
    <button class="hero-sample-thumb" data-file="${file}" title="Try &quot;${esc(label)}&quot; with Shop the Room">
      <img src="/assets/room-samples/${file}" alt="${esc(label)}" loading="lazy" />
    </button>`).join("");
  mount.querySelectorAll(".hero-sample-thumb").forEach(btn => {
    btn.addEventListener("click", async () => {
      const file = btn.dataset.file;
      const res = await fetch(`/assets/room-samples/${file}`);
      const blob = await res.blob();
      startShopTheRoom(new File([blob], file, { type: blob.type || "image/jpeg" }));
    });
  });
}

function wireVisualSearch(buttonId, inputId) {
  const btn = document.getElementById(buttonId);
  const input = document.getElementById(inputId);
  if (!btn || !input) return;

  btn.addEventListener("click", () => input.click());

  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";   // reset so picking the SAME file again still fires "change"
    if (!file) return;
    // stash the file in sessionStorage-free way: use an in-memory transfer via
    // the File object + navigate with a small delay isn't possible across pages,
    // so we upload immediately and pass results via a short-lived object URL +
    // a query flag; simplest robust approach: perform the search on this page
    // if a results container exists, otherwise redirect and re-trigger picker.
    startVisualSearch(file);
  });
}

// "Shop the Room" — same upload-and-navigate pattern as regular visual
// search, wired separately since it posts to a different endpoint and lands
// on its own results page.
function wireRoomSearch(buttonId, inputId) {
  const btn = document.getElementById(buttonId);
  const input = document.getElementById(inputId);
  if (!btn || !input) return;

  btn.addEventListener("click", () => input.click());

  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    startShopTheRoom(file);
  });
}

window.__pendingRoomFile = null;

function startShopTheRoom(file) {
  window.__pendingRoomFile = file;
  if (location.pathname.endsWith("shop-the-room.html")) {
    runShopTheRoom(file);
  } else {
    const reader = new FileReader();
    reader.onload = () => {
      try { sessionStorage.setItem("roomQueryImage", reader.result); } catch (e) {}
      location.href = "/shop-the-room.html";
    };
    reader.readAsDataURL(file);
  }
}

async function runShopTheRoom(file) {
  const statusEl = document.getElementById("room-status");
  const grid = document.getElementById("room-grid");
  const summaryEl = document.getElementById("room-summary");
  const thumb = document.getElementById("room-thumb");
  if (thumb && file) thumb.src = URL.createObjectURL(file);

  statusEl.textContent = "Scanning your room…";
  summaryEl.innerHTML = "";
  grid.innerHTML = `<div class="state-msg"><div class="spinner"></div>Finding one match per item…</div>`;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`${API_BASE}/api/shop-the-room`, { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Search failed");
    }
    const data = await res.json();
    if (!data.count) {
      statusEl.textContent = "No furnishings recognized";
      grid.innerHTML = `<div class="state-msg">We couldn't confidently match anything in this photo to the catalog. Try a photo with clearer, more separated items.</div>`;
      return;
    }
    statusEl.textContent = `Found ${data.count} item${data.count === 1 ? "" : "s"} for this room`;
    grid.innerHTML = data.items.map(p => `
      <div class="room-item-card">
        <span class="room-item-cat">${esc(prettifyCategory(p.category))}</span>
        ${productCardHTML(p)}
      </div>`).join("");
    wireProductCardInteractions(grid);
    summaryEl.innerHTML = `
      <div class="room-total-row">
        <div><strong>Room total:</strong> $${data.total_price.toFixed(2)} for ${data.count} item${data.count === 1 ? "" : "s"}</div>
        <button id="room-add-all" class="hero-cta room-add-all">Add all to cart</button>
      </div>`;
    document.getElementById("room-add-all")?.addEventListener("click", (e) => {
      data.items.forEach(p => addToCart(p.sku, 1));
      e.target.textContent = "Added ✓";
      setTimeout(() => { e.target.textContent = "Add all to cart"; }, 1500);
    });
  } catch (e) {
    statusEl.textContent = "Something went wrong.";
    grid.innerHTML = `<div class="state-msg">${e.message}.</div>`;
  }
}

function renderShopTheRoomPage() {
  const thumb = document.getElementById("room-thumb");
  const dataUrl = sessionStorage.getItem("roomQueryImage");
  if (dataUrl) {
    thumb.src = dataUrl;
    fetch(dataUrl).then(r => r.blob()).then(blob => {
      runShopTheRoom(new File([blob], "room.jpg", { type: blob.type || "image/jpeg" }));
    });
  } else if (window.__pendingRoomFile) {
    thumb.src = URL.createObjectURL(window.__pendingRoomFile);
    runShopTheRoom(window.__pendingRoomFile);
  } else {
    document.getElementById("room-status").textContent = "No photo received — go back and try Shop the Room again.";
  }
}

// Holds the in-flight query so the destination page can pick it up.
window.__pendingVisualSearchFile = null;

function startVisualSearch(file) {
  window.__pendingVisualSearchFile = file;
  if (location.pathname.endsWith("visual-search.html")) {
    runVisualSearch(file);
  } else {
    // Store file as a data URL in sessionStorage so it survives navigation.
    const reader = new FileReader();
    reader.onload = () => {
      try {
        sessionStorage.setItem("vsQueryImage", reader.result);
      } catch (e) {
        // sessionStorage quota fallback: still navigate, just no preview
      }
      location.href = "/visual-search.html";
    };
    reader.readAsDataURL(file);
  }
}

// Remembered so the category chip / refine box can re-run the same query
// (a photo OR a "find similar" sku — mutually exclusive, see runVisualSearch
// and runSimilarSearch) with a different scope/sort/refinement without
// asking the user to re-upload or re-click.
let _lastQueryFile = null;
let _lastQuerySku = null;
let _lastRefineText = "";

// The last successful result set (from an upload, a text refine, or a
// "find similar" call) plus the sort currently applied to it. Kept separate
// from the CLIP-side refinement on purpose: price isn't a visual concept
// CLIP was ever trained to ground (verified empirically — "cheaper" and "but
// in black" landed on nearly the same items when both were sent through the
// embedding), so a price intent is handled as a plain client-side sort over
// the already-computed matches, never as a new embedding call. This is also
// what makes chaining work correctly: "in black" changes WHICH items are the
// best matches (re-ranks via CLIP); "cheaper" only changes the ORDER you see
// them in (sorts), without discarding the visual refinement already applied.
let _lastResultData = null;
let _currentSort = "match";      // "match" | "price-asc" | "price-desc"
let _currentMaxPrice = null;     // set by an explicit "under $X" refinement; null = no cap

// Parses a refine-box string into its price-intent parts and whatever visual
// text is left over. Two things this fixes vs. a bare keyword match:
//   1. "under $50" previously just triggered an ascending sort — the number
//      was matched by the regex but never actually used to filter anything,
//      so a $300 item could still show up. Now it's captured and applied as
//      a real price cap.
//   2. A compound query like "cheaper black one" previously matched the
//      price branch and returned early, silently dropping "black" — the
//      CLIP refinement never ran. Now the price phrase is stripped out and
//      whatever visual text remains is still sent through as a refinement.
// Returns { sort, maxPrice, residualText }. sort/maxPrice are null when no
// price intent was found at all; residualText is "" when the whole input
// was a price phrase (safe to treat as sort-only, no network call needed).
function _parsePriceIntent(text) {
  let t = text;
  let sort = null;
  let maxPrice = null;

  const underMatch = t.match(/\b(?:under|below|less than)\s*\$?(\d+(?:\.\d+)?)\b/i);
  if (underMatch) {
    maxPrice = parseFloat(underMatch[1]);
    sort = "price-asc";
    t = t.slice(0, underMatch.index) + t.slice(underMatch.index + underMatch[0].length);
  } else {
    const cheapMatch = t.match(/\b(cheap(er|est)?|budget|affordable|inexpensive|lower[- ]?price|less expensive)\b/i);
    if (cheapMatch) {
      sort = "price-asc";
      t = t.slice(0, cheapMatch.index) + t.slice(cheapMatch.index + cheapMatch[0].length);
    } else {
      const pricyMatch = t.match(/\b(expensive|pricier|pricey|premium|higher[- ]?end|luxury|splurge|top[- ]?shelf)\b/i);
      if (pricyMatch) {
        sort = "price-desc";
        t = t.slice(0, pricyMatch.index) + t.slice(pricyMatch.index + pricyMatch[0].length);
      }
    }
  }

  // Tidy up connector words/punctuation left behind by stripping the price
  // phrase ("cheaper one" -> "one", "cheaper, black" -> ", black").
  const residualText = t.replace(/\b(one|item|version|option|please|thanks?)\b/gi, "")
    .replace(/^[\s,.-]+|[\s,.-]+$/g, "").replace(/\s{2,}/g, " ").trim();

  return { sort, maxPrice, residualText };
}

function _sortedItems() {
  let items = (_lastResultData && _lastResultData.items) || [];
  if (_currentMaxPrice != null) items = items.filter(p => p.price <= _currentMaxPrice);
  if (_currentSort === "price-asc") return [...items].sort((a, b) => a.price - b.price);
  if (_currentSort === "price-desc") return [...items].sort((a, b) => b.price - a.price);
  return items;   // "match" — server already returns items in match-score order
}

// Re-renders the current result set under whatever sort is active, with NO
// network call — used both right after a real search and whenever the sort
// control changes.
function applySortAndRender() {
  const grid = document.getElementById("listing-grid");
  const statusEl = document.getElementById("vs-status");
  if (!grid || !statusEl || !_lastResultData) return;
  renderVisualResults(grid, statusEl, { ..._lastResultData, items: _sortedItems() });
  renderSortControl();
}

function renderSortControl() {
  const mount = document.getElementById("vs-sort");
  if (!mount || !_lastResultData || !(_lastResultData.items || []).length) { if (mount) mount.innerHTML = ""; return; }
  const priceNote = _currentMaxPrice != null
    ? `<span class="vs-sort-cap">under $${_currentMaxPrice.toFixed(0)} <button type="button" id="vs-sort-cap-clear" title="Remove price cap">×</button></span>`
    : "";
  mount.innerHTML = `
    <label class="vs-sort-label">Sort
      <select id="vs-sort-select">
        <option value="match"${_currentSort === "match" ? " selected" : ""}>Best match</option>
        <option value="price-asc"${_currentSort === "price-asc" ? " selected" : ""}>Price: Low to High</option>
        <option value="price-desc"${_currentSort === "price-desc" ? " selected" : ""}>Price: High to Low</option>
      </select>
    </label>${priceNote}`;
  mount.querySelector("#vs-sort-select").addEventListener("change", (e) => {
    _currentSort = e.target.value;
    _currentMaxPrice = null;   // the dropdown re-sorts the full set; only the text box sets a cap
    applySortAndRender();
  });
  mount.querySelector("#vs-sort-cap-clear")?.addEventListener("click", () => {
    _currentMaxPrice = null;
    applySortAndRender();
  });
}

async function runVisualSearch(file, opts = {}) {
  _lastQueryFile = file;
  _lastQuerySku = null;   // mutually exclusive with "find similar" mode — see runSimilarSearch
  const scope = opts.scope || "auto";
  const refineText = opts.refineText !== undefined ? opts.refineText : _lastRefineText;
  _lastRefineText = refineText;
  _currentSort = opts.sort || "match";               // a real (re-)search starts from best-match order...
  _currentMaxPrice = opts.maxPrice != null ? opts.maxPrice : null;   // ...unless a compound refine ("cheaper black one") carried a price intent along with it
  const statusEl = document.getElementById("vs-status");
  const grid = document.getElementById("listing-grid");

  // Always sync the "Your photo" preview to the image actually being searched —
  // covers in-page re-searches from the header search bar, not just page load.
  const thumb = document.getElementById("query-thumb");
  if (thumb && file) thumb.src = URL.createObjectURL(file);

  statusEl.textContent = "Analyzing your photo…";
  _clearVsExtra();
  grid.innerHTML = `<div class="state-msg"><div class="spinner"></div>Finding similar products…</div>`;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const qs = new URLSearchParams({ scope });
    if (refineText) qs.set("refine_text", refineText);
    const res = await fetch(`${API_BASE}/api/visual-search?${qs}`, { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Search failed");
    }
    const data = await res.json();
    _lastResultData = data;
    renderCategoryChip(data);
    applySortAndRender();
    renderRefineBox(data);
  } catch (e) {
    _clearVsExtra();
    statusEl.textContent = "Something went wrong analyzing that photo.";
    grid.innerHTML = `<div class="state-msg">${e.message}. Try a different image.</div>`;
  }
}

// "but in black" / "cheaper" / "cheaper black one" text box. Pure visual
// text ("in black") blends a CLIP text embedding into the query and
// re-ranks (see backend's refine_text param). Pure price text ("cheaper",
// "under $50") is parsed and just re-sorts/filters the CURRENT results —
// see _parsePriceIntent above for why these are handled by entirely
// different mechanisms. A COMPOUND query ("cheaper black one") does both:
// the price phrase is stripped out and applied as a sort/cap, and whatever
// visual text remains still gets sent through as a real refinement instead
// of being silently dropped. Rendered fresh after every search so it
// reflects the current photo/refinement state.
// Re-runs whichever query is currently active — an uploaded photo or a
// "find similar" sku are mutually exclusive (see runVisualSearch /
// runSimilarSearch), so the refine box doesn't need to know which mode
// it's in; it just re-fires the same one with new options.
function _reRunCurrentQuery(opts) {
  if (_lastQuerySku) runSimilarSearch(_lastQuerySku, opts);
  else if (_lastQueryFile) runVisualSearch(_lastQueryFile, opts);
}

function renderRefineBox(data) {
  const mount = document.getElementById("vs-refine");
  if (!mount) return;
  mount.innerHTML = `
    <form id="vs-refine-form" class="vs-refine-form" autocomplete="off">
      <input id="vs-refine-input" type="text" placeholder="Refine with text — e.g. &quot;but in black&quot; or &quot;cheaper&quot;" value="${esc(_lastRefineText)}" />
      <button type="submit">Refine</button>
      ${_lastRefineText ? `<button type="button" id="vs-refine-clear">Clear</button>` : ""}
    </form>`;
  mount.querySelector("#vs-refine-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = mount.querySelector("#vs-refine-input").value.trim();
    if (!text) { _reRunCurrentQuery({ refineText: "" }); return; }

    const { sort, maxPrice, residualText } = _parsePriceIntent(text);
    if (sort) {
      if (residualText) {
        // Compound: apply the price sort/cap AND send the leftover visual
        // text through as a real refinement, instead of dropping it.
        _reRunCurrentQuery({ refineText: residualText, sort, maxPrice });
      } else {
        // Price-only: sort-in-place, leaves _lastRefineText (and thus the
        // underlying match set) untouched, so a prior visual refinement
        // like "in black" survives.
        _currentSort = sort;
        _currentMaxPrice = maxPrice;
        applySortAndRender();
      }
      return;
    }
    _reRunCurrentQuery({ refineText: text });
  });
  mount.querySelector("#vs-refine-clear")?.addEventListener("click", () => {
    _reRunCurrentQuery({ refineText: "" });
  });
}

// Split results by the server's match_threshold: strong matches shown, weak ones
// tucked behind an expander. If nothing clears the bar, show an honest empty state
// (the uploaded item probably isn't in the catalog).
function renderVisualResults(grid, statusEl, data) {
  _clearVsExtra();
  const items = data.items || [];
  const scopeNote = data.scoped ? ` in ${prettifyCategory(data.predicted_category)}` : "";
  const lowConf = data.confidence != null && data.confidence < 30;

  const statsEl = document.getElementById("vs-stats");
  if (statsEl) {
    statsEl.textContent = (data.searched != null && data.took_ms != null)
      ? `Ranked ${data.searched.toLocaleString()} products in ${data.took_ms}ms`
      : "";
  }

  // match_threshold is only calibrated for the "image" score scale. If the
  // server ever falls back to displaying the raw fused score (score_basis ===
  // "fused" — a rare fallback, e.g. an older index cache without image_vecs),
  // that threshold would misfire (a genuine match reads far lower on the fused
  // scale), so don't filter at all in that case — show everything as-is.
  const thresholdApplies = data.score_basis !== "fused";
  const th = thresholdApplies ? (data.match_threshold ?? 0) : -Infinity;
  const strong = items.filter(p => (p.match_score ?? 0) >= th);
  const weak = items.filter(p => (p.match_score ?? 0) < th);

  // The backend now fetches a wider pool than the grid displays by default
  // (see config.yaml's search.top_k) so sort/price/text refinements have real
  // material to reorder instead of just shuffling a handful of tiles. Cap the
  // main grid to a clean row-and-a-bit and fold any overflow into the same
  // collapsible section as the below-threshold matches.
  const DISPLAY_CAP = 12;
  const shown = strong.slice(0, DISPLAY_CAP);
  const more = strong.slice(DISPLAY_CAP).concat(weak);

  if (shown.length) {
    statusEl.textContent = `Found ${strong.length} strong match${strong.length === 1 ? "" : "es"}${scopeNote}`;
    grid.innerHTML = shown.map(p => productCardHTML(p)).join("");
    wireProductCardInteractions(grid);
    if (more.length) _appendWeakSection(more, `Show ${more.length} more match${more.length === 1 ? "" : "es"}`);
  } else {
    statusEl.textContent = "No strong visual matches";
    grid.innerHTML = `
      <div class="vs-empty">
        <div class="vs-empty-badge">0 strong matches</div>
        <h2>No strong visual matches</h2>
        <p>We couldn't find products that closely match your photo — it may not be in this catalog${lowConf ? ", and we couldn't confidently categorize it" : ""}. Try a clearer photo, or browse a category.</p>
      </div>`;
    if (more.length) _appendWeakSection(more, `Show closest matches anyway (${more.length})`);
  }
}

function _clearVsExtra() {
  document.getElementById("vs-extra")?.remove();
}

// A collapsible section (button + hidden grid) for the below-threshold matches.
function _appendWeakSection(weak, showLabel) {
  const grid = document.getElementById("listing-grid");
  if (!grid) return;
  const wrap = document.createElement("div");
  wrap.id = "vs-extra";
  wrap.innerHTML = `
    <div class="vs-more-wrap"><button class="vs-expand-btn" aria-expanded="false">${showLabel}</button></div>
    <div class="product-grid vs-weak-grid" hidden></div>`;
  grid.after(wrap);
  const weakGrid = wrap.querySelector(".vs-weak-grid");
  const btn = wrap.querySelector(".vs-expand-btn");
  let built = false;
  btn.addEventListener("click", () => {
    const opening = weakGrid.hidden;
    if (opening && !built) {                       // build + wire the weak cards on first open
      weakGrid.innerHTML = weak.map(p => productCardHTML(p)).join("");
      wireProductCardInteractions(weakGrid);
      built = true;
    }
    weakGrid.hidden = !opening;
    btn.setAttribute("aria-expanded", String(opening));
    btn.textContent = opening ? "Hide lower-confidence matches" : showLabel;
  });
}

// The "Detected: <category>" chip — a soft, user-overridable category filter.
function renderCategoryChip(data) {
  const chip = document.getElementById("vs-category-chip");
  if (!chip) return;
  if (!data.predicted_category) { chip.hidden = true; return; }

  const label = prettifyCategory(data.predicted_category);
  const conf = data.confidence != null ? `${data.confidence}%` : "";
  if (data.scoped) {
    chip.innerHTML =
      `<span class="chip-dot"></span>Detected <strong>${label}</strong> · ${conf} confidence` +
      ` — showing ${label} only <button class="chip-link" data-scope="all">See all categories</button>`;
  } else {
    chip.innerHTML =
      `<span class="chip-dot soft"></span>Looks like <strong>${label}</strong> · ${conf}` +
      ` — showing all categories <button class="chip-link" data-scope="force">Show ${label} only</button>`;
  }
  chip.hidden = false;
  const btn = chip.querySelector(".chip-link");
  if (btn) btn.addEventListener("click", () => {
    if (_lastQueryFile) runVisualSearch(_lastQueryFile, { scope: btn.dataset.scope });
  });
}

// "Find similar" entry point (product cards / PDP link to
// visual-search.html?sku=...) — queries FROM an existing catalog product's
// own vector instead of an uploaded photo. No file, no embedding call.
// Supports the same refine/sort/price-cap options as runVisualSearch — the
// refine box used to be hidden entirely here (text refinement was only
// wired up for an uploaded photo), but the backend's refine logic never
// actually cared where the "pure image" vector came from, so there was no
// real reason "find similar, but in black" couldn't work identically to
// refining an upload.
async function runSimilarSearch(sku, opts = {}) {
  _lastQuerySku = sku;
  _lastQueryFile = null;   // mutually exclusive with photo-search mode — see runVisualSearch
  const refineText = opts.refineText !== undefined ? opts.refineText : _lastRefineText;
  _lastRefineText = refineText;
  _currentSort = opts.sort || "match";
  _currentMaxPrice = opts.maxPrice != null ? opts.maxPrice : null;

  const statusEl = document.getElementById("vs-status");
  const grid = document.getElementById("listing-grid");
  const thumb = document.getElementById("query-thumb");
  const hint = document.querySelector(".vs-query-hint");
  const chip = document.getElementById("vs-category-chip");
  const statsEl = document.getElementById("vs-stats");

  if (chip) chip.hidden = true;
  statusEl.textContent = "Finding similar products…";
  _clearVsExtra();
  grid.innerHTML = `<div class="state-msg"><div class="spinner"></div>Finding similar products…</div>`;

  try {
    const pRes = await fetch(`${API_BASE}/api/products/${encodeURIComponent(sku)}`);
    if (!pRes.ok) throw new Error("Product not found");
    const product = await pRes.json();
    if (thumb) thumb.src = product.image_url;
    if (hint) hint.textContent = `Products that visually match "${product.name}".`;

    const qs = new URLSearchParams();
    if (refineText) qs.set("refine_text", refineText);
    const t0 = performance.now();
    const res = await fetch(`${API_BASE}/api/products/${encodeURIComponent(sku)}/similar?${qs}`);
    if (!res.ok) throw new Error("Could not find similar products");
    const data = await res.json();
    const tookMs = Math.round(performance.now() - t0);

    if (statsEl) statsEl.textContent = data.count ? `Found ${data.count} similar products in ${tookMs}ms` : "";
    if (data.count) {
      // renderVisualResults (via applySortAndRender) sets statusEl itself —
      // no match_threshold on this endpoint's response, so every item counts
      // as a "strong match" there, which reads fine for a find-similar context.
      _lastResultData = data;
      applySortAndRender();
    } else {
      _lastResultData = null;
      statusEl.textContent = "No similar products found";
      grid.innerHTML = `<div class="state-msg">Couldn't find anything visually similar to this product.</div>`;
    }
    renderRefineBox(data);
  } catch (e) {
    statusEl.textContent = "Something went wrong.";
    grid.innerHTML = `<div class="state-msg">${e.message}.</div>`;
  }
}

// ---------- Crop-to-search ----------
// Lets the user draw a box over their uploaded photo and search using just
// that region — solves the multi-object case (a room photo with a sofa AND
// a lamp) a single whole-image embed can't disentangle, with no new model:
// crop client-side on a canvas, then re-embed the crop through the exact
// same endpoint as a normal upload.
let _cropModalBuilt = false;
let _cropRect = null;   // {x,y,w,h} in the crop-stage's rendered pixel space

function _buildCropModal() {
  if (_cropModalBuilt) return;
  _cropModalBuilt = true;
  const modal = document.createElement("div");
  modal.id = "vs-crop-modal";
  modal.className = "vs-crop-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="vs-crop-modal-inner">
      <div class="vs-crop-stage" id="vs-crop-stage">
        <img id="vs-crop-img" alt="" draggable="false" />
        <div id="vs-crop-box" class="vs-crop-box" hidden></div>
      </div>
      <p class="vs-crop-hint">Drag a box around just the item you want to search for.</p>
      <div class="vs-crop-modal-actions">
        <button id="vs-crop-cancel" type="button">Cancel</button>
        <button id="vs-crop-confirm" type="button" disabled>Search this crop</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const stage = modal.querySelector("#vs-crop-stage");
  const box = modal.querySelector("#vs-crop-box");
  const confirmBtn = modal.querySelector("#vs-crop-confirm");
  let dragging = false, startX = 0, startY = 0;

  // Pointer Events unify mouse/touch/pen in one set of handlers (the
  // original mouse-only version was dead on a tablet or touchscreen
  // laptop). Pointer capture keeps move/up events targeted at the stage
  // even if the finger/cursor drifts outside its bounds mid-drag, so these
  // can live on the stage element itself instead of needing permanent
  // window-level listeners.
  stage.addEventListener("pointerdown", (e) => {
    stage.setPointerCapture(e.pointerId);
    const r = stage.getBoundingClientRect();
    startX = e.clientX - r.left;
    startY = e.clientY - r.top;
    dragging = true;
    box.hidden = false;
    Object.assign(box.style, { left: `${startX}px`, top: `${startY}px`, width: "0px", height: "0px" });
    confirmBtn.disabled = true;
    e.preventDefault();
  });
  stage.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const r = stage.getBoundingClientRect();
    const curX = Math.max(0, Math.min(e.clientX - r.left, r.width));
    const curY = Math.max(0, Math.min(e.clientY - r.top, r.height));
    const x = Math.min(startX, curX), y = Math.min(startY, curY);
    const w = Math.abs(curX - startX), h = Math.abs(curY - startY);
    Object.assign(box.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
    _cropRect = { x, y, w, h };
  });
  stage.addEventListener("pointerup", (e) => {
    if (!dragging) return;
    dragging = false;
    stage.releasePointerCapture(e.pointerId);
    confirmBtn.disabled = !_cropRect || _cropRect.w < 20 || _cropRect.h < 20;
  });

  modal.querySelector("#vs-crop-cancel").addEventListener("click", () => { modal.hidden = true; });

  confirmBtn.addEventListener("click", () => {
    const img = modal.querySelector("#vs-crop-img");
    const stageRect = stage.getBoundingClientRect();
    // Map the drawn box (rendered-pixel space) back to the image's natural
    // pixel space. The <img> is object-fit:contain inside the stage, so it's
    // letterboxed — account for that offset/scale rather than assuming 1:1.
    const scale = Math.min(stageRect.width / img.naturalWidth, stageRect.height / img.naturalHeight);
    const dispW = img.naturalWidth * scale, dispH = img.naturalHeight * scale;
    const offX = (stageRect.width - dispW) / 2, offY = (stageRect.height - dispH) / 2;
    const sx = Math.max(0, (_cropRect.x - offX) / scale);
    const sy = Math.max(0, (_cropRect.y - offY) / scale);
    const sw = Math.min(img.naturalWidth - sx, _cropRect.w / scale);
    const sh = Math.min(img.naturalHeight - sy, _cropRect.h / scale);
    if (sw < 4 || sh < 4) return;

    const canvas = document.createElement("canvas");
    canvas.width = sw; canvas.height = sh;
    canvas.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
    canvas.toBlob((blob) => {
      if (!blob) return;
      modal.hidden = true;
      runVisualSearch(new File([blob], "crop.png", { type: "image/png" }), { refineText: "" });
    }, "image/png");
  });
}

function openCropToSearch() {
  const src = document.getElementById("query-thumb")?.src;
  if (!src) return;
  _buildCropModal();
  const modal = document.getElementById("vs-crop-modal");
  modal.querySelector("#vs-crop-img").src = src;
  const box = modal.querySelector("#vs-crop-box");
  box.hidden = true;
  _cropRect = null;
  modal.querySelector("#vs-crop-confirm").disabled = true;
  modal.hidden = false;
}

function wireCropToggle(buttonId) {
  document.getElementById(buttonId)?.addEventListener("click", openCropToSearch);
}

// ---------- Scan to try on your phone (QR + mobile capture) ----------
// Desktop side: a button opens a modal with a QR code pointing back at this
// same app with ?mobilescan=1. Mobile side: renderMobileScanLanding (called
// unconditionally on page load) checks for that flag and, if present,
// replaces the page with a single big camera button — tap it, take a photo,
// and it runs the exact same search pipeline as the desktop upload flow
// (startVisualSearch), just entered from a phone's actual camera instead of
// a file picker.
let _scanModalBuilt = false;

function buildScanModal() {
  if (_scanModalBuilt) return;
  _scanModalBuilt = true;
  const modal = document.createElement("div");
  modal.id = "vs-scan-modal";
  modal.className = "vs-scan-modal";
  modal.hidden = true;
  modal.innerHTML = `
    <div class="vs-scan-modal-inner">
      <h3>Scan to try on your phone</h3>
      <p>Open your phone's camera app and point it at the code below.</p>
      <div id="vs-scan-qr"></div>
      <div class="vs-scan-url" id="vs-scan-url"></div>
      <button id="vs-scan-close" type="button">Close</button>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector("#vs-scan-close").addEventListener("click", () => { modal.hidden = true; });
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
}

function openScanModal() {
  buildScanModal();
  const modal = document.getElementById("vs-scan-modal");
  const url = `${location.origin}/?mobilescan=1`;
  const qrEl = document.getElementById("vs-scan-qr");
  qrEl.innerHTML = "";
  new QRCode(qrEl, { text: url, width: 200, height: 200, correctLevel: QRCode.CorrectLevel.M });
  document.getElementById("vs-scan-url").textContent = url;
  modal.hidden = false;
}

function wireScanToggle(buttonId) {
  document.getElementById(buttonId)?.addEventListener("click", openScanModal);
}

// Called on every page load (cheap no-op unless ?mobilescan=1 is present) —
// see index.html.
function renderMobileScanLanding() {
  if (new URLSearchParams(location.search).get("mobilescan") !== "1") return;

  const overlay = document.createElement("div");
  overlay.className = "vs-mobile-scan";
  overlay.innerHTML = `
    <h1>Search by photo</h1>
    <p>Tap the button, take a photo of anything for your home or office, and we'll find it in the catalog.</p>
    <button id="vs-mobile-scan-btn" class="vs-mobile-scan-btn" type="button" aria-label="Open camera">
      <svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M4 8a2 2 0 0 1 2-2h1.5l1-1.5h7l1 1.5H18a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8z"/>
        <circle cx="12" cy="12.5" r="3.5"/>
      </svg>
    </button>
    <span class="vs-mobile-scan-label">Open Camera</span>
    <input id="vs-mobile-scan-input" type="file" accept="image/*" capture="environment" hidden />
  `;
  document.body.appendChild(overlay);

  const input = overlay.querySelector("#vs-mobile-scan-input");
  overlay.querySelector("#vs-mobile-scan-btn").addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    startVisualSearch(file);
  });
}

// ---------- Global image drop / paste-to-search ----------
// Works on every page (wired from renderSiteChrome) — drag an image file
// anywhere on the site, or paste one from the clipboard, and it runs the
// same search an upload would.
function wireGlobalImageDrop() {
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    dragDepth++;
    document.body.classList.add("vs-dragging");
  });
  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) document.body.classList.remove("vs-dragging");
  });
  window.addEventListener("dragover", (e) => {
    if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault();
  });
  window.addEventListener("drop", (e) => {
    dragDepth = 0;
    document.body.classList.remove("vs-dragging");
    const file = e.dataTransfer?.files?.[0];
    if (file && file.type.startsWith("image/")) {
      e.preventDefault();
      startVisualSearch(file);
    }
  });
  window.addEventListener("paste", (e) => {
    const item = [...(e.clipboardData?.items || [])].find(i => i.type && i.type.startsWith("image/"));
    if (item) {
      const file = item.getAsFile();
      if (file) startVisualSearch(file);
    }
  });
}

function renderVisualSearchResultsPage() {
  const skuParam = new URLSearchParams(location.search).get("sku");
  if (skuParam) { runSimilarSearch(skuParam); return; }

  const thumb = document.getElementById("query-thumb");
  const dataUrl = sessionStorage.getItem("vsQueryImage");
  if (dataUrl) {
    thumb.src = dataUrl;
    // convert back to a File so we can POST it
    fetch(dataUrl).then(r => r.blob()).then(blob => {
      const file = new File([blob], "query.png", { type: blob.type || "image/png" });
      runVisualSearch(file);
    });
  } else if (window.__pendingVisualSearchFile) {
    thumb.src = URL.createObjectURL(window.__pendingVisualSearchFile);
    runVisualSearch(window.__pendingVisualSearchFile);
  } else {
    document.getElementById("vs-status").textContent = "No photo received — go back and try Visual Search again.";
    document.getElementById("listing-grid").innerHTML = "";
  }
}

function wireTextSearch() {
  const form = document.getElementById("text-search-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = document.getElementById("text-search-input").value.trim();
    // Route to the listing page as a real text-search query (backend /api/search).
    location.href = q ? `/category.html?q=${encodeURIComponent(q)}` : "/category.html";
  });
}

// ---------- Product detail page ----------
async function renderProductDetail(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  const sku = new URLSearchParams(location.search).get("sku");
  if (!sku) {
    el.innerHTML = `<div class="state-msg">No product specified.</div>`;
    return;
  }
  el.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading product…</div>`;
  try {
    const res = await fetch(`${API_BASE}/api/products/${encodeURIComponent(sku)}`);
    if (res.status === 404) throw new Error("Product not found");
    if (!res.ok) throw new Error("Could not load product");
    const p = await res.json();
    document.title = `${p.name} | Staples`;
    const catLabel = prettifyCategory(p.category);
    el.innerHTML = `
      <nav class="breadcrumb">
        <a href="/">Home</a> ›
        <a href="/category.html?category=${encodeURIComponent(p.category)}">${esc(catLabel)}</a> ›
        <span>${esc(p.name)}</span>
      </nav>
      <div class="pdp">
        <div class="pdp-media">
          <img src="${esc(p.image_url)}" alt="${esc(p.name)}" />
          <a class="pdp-find-similar" href="/visual-search.html?sku=${encodeURIComponent(p.sku)}">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
            Find visually similar
          </a>
        </div>
        <div class="pdp-info">
          <p class="pdp-brand">${esc(p.brand || "Staples")}</p>
          <h1 class="pdp-name">${esc(p.name)}</h1>
          <div class="stars">${starString(p.rating)} <span class="count">${p.rating} · ${p.reviews} reviews</span></div>
          <div class="pdp-price-row">
            <span class="price">$${p.price.toFixed(2)}</span>
            <span class="list-price">$${p.list_price.toFixed(2)}</span>
            <span class="pdp-savings">Save ${p.savings_pct}%</span>
          </div>
          <p class="pdp-desc">${esc(p.description || "")}</p>
          <div class="pdp-actions">
            <div class="qty-stepper">
              <button class="qty-dec" aria-label="Decrease">−</button>
              <span class="qty-val">1</span>
              <button class="qty-inc" aria-label="Increase">+</button>
            </div>
            <button class="add-btn pdp-add">Add to Cart</button>
          </div>
          <ul class="pdp-perks">
            <li>✓ Free shipping on orders $45+</li>
            <li>✓ Free & easy returns</li>
            <li>✓ SKU: ${p.sku}</li>
          </ul>
        </div>
      </div>`;

    // wire qty + add
    let qty = 1;
    const qtyVal = el.querySelector(".qty-val");
    el.querySelector(".qty-inc").addEventListener("click", () => { qty++; qtyVal.textContent = qty; });
    el.querySelector(".qty-dec").addEventListener("click", () => { if (qty > 1) { qty--; qtyVal.textContent = qty; } });
    const addBtn = el.querySelector(".pdp-add");
    addBtn.addEventListener("click", () => {
      addToCart(p.sku, qty);
      addBtn.textContent = "Added ✓";
      addBtn.classList.add("added");
      setTimeout(() => { addBtn.textContent = "Add to Cart"; addBtn.classList.remove("added"); }, 1200);
    });

    // "You may also like" — real visual similarity (this product's own
    // vector), not just same-category. "Similar for less" reuses the same
    // neighborhood, filtered to strictly cheaper items.
    renderSimilarRail("related-rail", p.sku);
    renderSimilarRail("cheaper-rail", p.sku, { maxPrice: p.price });
  } catch (e) {
    el.innerHTML = `<div class="state-msg">${e.message}. <a href="/">Back to home</a></div>`;
  }
}

// ---------- Cart page ----------
// The cart itself (getCart/setCart/addToCart/...) only ever stores {sku:
// qty} — no product details — so a dedicated page fetching those details is
// what actually makes it reviewable/editable instead of just a number badge.

function cartLineItemHTML(p) {
  const href = `/product.html?sku=${encodeURIComponent(p.sku)}`;
  return `
    <div class="cart-line" data-sku="${p.sku}">
      <a href="${href}" class="cart-line-media"><img src="${esc(p.image_url)}" alt="${esc(p.name)}" /></a>
      <div class="cart-line-info">
        <a href="${href}" class="cart-line-name">${esc(p.name)}</a>
        <div class="cart-line-price">$${p.price.toFixed(2)} each</div>
        <div class="cart-line-actions">
          <div class="qty-stepper">
            <button class="qty-dec" aria-label="Decrease quantity">−</button>
            <span class="qty-val">${p.qty}</span>
            <button class="qty-inc" aria-label="Increase quantity">+</button>
          </div>
          <button class="cart-remove-btn">Remove</button>
        </div>
      </div>
      <div class="cart-line-total">$${(p.price * p.qty).toFixed(2)}</div>
    </div>`;
}

async function renderCartPage(elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = `<div class="state-msg"><div class="spinner"></div>Loading your cart…</div>`;

  // One fetch per distinct sku (carts are small — this is not the N+1
  // listing-endpoint mistake fixed elsewhere in this app, just a handful of
  // parallel requests for a handful of line items) then everything after is
  // redrawn from the in-memory `items` array — no re-fetching on every qty
  // tweak.
  const cart = getCart();
  const skus = Object.keys(cart);
  const fetched = await Promise.all(skus.map(sku =>
    fetch(`${API_BASE}/api/products/${encodeURIComponent(sku)}`).then(r => r.ok ? r.json() : null).catch(() => null)
  ));
  const valid = fetched.filter(Boolean);
  const validSkus = new Set(valid.map(p => p.sku));
  const stale = skus.filter(s => !validSkus.has(s));   // catalog changed under an old cart — prune quietly
  if (stale.length) {
    const c = getCart();
    stale.forEach(s => delete c[s]);
    setCart(c);
  }
  let items = valid.map(p => ({ ...p, qty: cart[p.sku] }));

  function draw() {
    if (!items.length) {
      el.innerHTML = `
        <div class="cart-empty">
          <h1>Your cart is empty</h1>
          <p>Find something you like with a photo, or browse the catalog.</p>
          <a href="/" class="hero-cta">Continue shopping</a>
        </div>`;
      return;
    }
    const totalQty = items.reduce((a, p) => a + p.qty, 0);
    const subtotal = items.reduce((a, p) => a + p.price * p.qty, 0);
    const shipping = subtotal >= 45 ? 0 : 5.99;
    el.innerHTML = `
      <div class="cart-layout">
        <div class="cart-items">
          <h1>Your cart <span class="cart-items-count">(${totalQty} item${totalQty === 1 ? "" : "s"})</span></h1>
          ${items.map(cartLineItemHTML).join("")}
        </div>
        <aside class="cart-summary">
          <h2>Order summary</h2>
          <div class="cart-summary-row"><span>Subtotal</span><span>$${subtotal.toFixed(2)}</span></div>
          <div class="cart-summary-row"><span>Shipping</span><span>${shipping === 0 ? "FREE" : `$${shipping.toFixed(2)}`}</span></div>
          <div class="cart-summary-row cart-summary-total"><span>Total</span><span>$${(subtotal + shipping).toFixed(2)}</span></div>
          <button class="hero-cta cart-checkout-btn">Checkout</button>
          <p class="cart-summary-note">Demo only — checkout isn't wired up to anything.</p>
        </aside>
      </div>`;

    el.querySelectorAll(".cart-line").forEach(line => {
      const sku = line.dataset.sku;
      const item = items.find(p => p.sku === sku);
      const update = (newQty) => {
        setCartItemQty(sku, newQty);
        if (newQty <= 0) items = items.filter(p => p.sku !== sku);
        else item.qty = newQty;
        draw();
      };
      line.querySelector(".qty-inc").addEventListener("click", () => update(item.qty + 1));
      line.querySelector(".qty-dec").addEventListener("click", () => update(item.qty - 1));
      line.querySelector(".cart-remove-btn").addEventListener("click", () => update(0));
    });
    el.querySelector(".cart-checkout-btn")?.addEventListener("click", () => {
      alert(`This is a demo, so checkout isn't wired up — but your total ($${(subtotal + shipping).toFixed(2)}) was calculated correctly.`);
    });
  }

  draw();
}
