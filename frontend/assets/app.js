// Staples visual search demo — vanilla JS, no build step needed.

const API_BASE = ""; // same-origin, FastAPI serves both API and frontend

// ---------- Persistent cart (localStorage) ----------
const CART_KEY = "vsCartCount";

function getCartCount() {
  return parseInt(localStorage.getItem(CART_KEY) || "0", 10) || 0;
}
function setCartCount(n) {
  localStorage.setItem(CART_KEY, String(n));
  document.querySelectorAll("#cart-count").forEach(el => { el.textContent = n; });
}
function addToCart(qty) {
  setCartCount(getCartCount() + qty);
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
            <a class="menu-feature" href="/architecture.html">
              <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 21h18"/><path d="M5 21V9l7-5 7 5v12"/><path d="M9 21v-6h6v6"/></svg>
              <span><strong>Architecture Overview</strong><small>How it works — local &amp; GCP, explained visually</small></span>
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
          <button class="acct-link cart-btn" title="Cart" aria-label="Cart">
            ${ICONS.cart}<span>Cart</span>
            <span id="cart-count" class="cart-badge">${getCartCount()}</span>
          </button>
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
      <p>© 1998–${year} Staples, Inc. — <strong>Demo prototype, not affiliated with Staples, Inc.</strong> All product names &amp; images are fictional placeholders.</p>
      <p class="footer-legal"><a href="#">Site Map</a> · <a href="#">Privacy Notice</a> · <a href="#">Terms &amp; Conditions</a> · <a href="#">California Notice</a></p>
    </div>
  </footer>`;
}

// Convenience: render both, then return so callers can wire page behavior.
function renderSiteChrome() {
  renderSiteHeader();
  renderSiteFooter();
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
      <a class="product-media" href="${href}"><img src="${p.image_url}" alt="${p.name}" loading="lazy" /></a>
      <a class="product-name" href="${href}">${p.name}</a>
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
      addToCart(qty);
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
        grid.innerHTML = `<div class="state-msg">No products matched${_listing.query ? ` “${_listing.query}”` : ""}. Try a different search or browse a category.</div>`;
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

function wireVisualSearch(buttonId, inputId) {
  const btn = document.getElementById(buttonId);
  const input = document.getElementById(inputId);
  if (!btn || !input) return;

  btn.addEventListener("click", () => input.click());

  input.addEventListener("change", () => {
    const file = input.files[0];
    if (!file) return;
    // stash the file in sessionStorage-free way: use an in-memory transfer via
    // the File object + navigate with a small delay isn't possible across pages,
    // so we upload immediately and pass results via a short-lived object URL +
    // a query flag; simplest robust approach: perform the search on this page
    // if a results container exists, otherwise redirect and re-trigger picker.
    startVisualSearch(file);
  });
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

// Remembered so the category chip can re-run the same photo with a different scope.
let _lastQueryFile = null;

async function runVisualSearch(file, opts = {}) {
  _lastQueryFile = file;
  const scope = opts.scope || "auto";
  const statusEl = document.getElementById("vs-status");
  const grid = document.getElementById("listing-grid");

  // Always sync the "Your photo" preview to the image actually being searched —
  // covers in-page re-searches from the header search bar, not just page load.
  const thumb = document.getElementById("query-thumb");
  if (thumb && file) thumb.src = URL.createObjectURL(file);

  statusEl.textContent = "Analyzing your photo…";
  grid.innerHTML = `<div class="state-msg"><div class="spinner"></div>Finding similar products…</div>`;

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(`${API_BASE}/api/visual-search?scope=${scope}`, { method: "POST", body: formData });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Search failed");
    }
    const data = await res.json();
    const scopeNote = data.scoped ? ` in ${prettifyCategory(data.predicted_category)}` : "";
    statusEl.textContent = `Found ${data.count} similar product${data.count === 1 ? "" : "s"}${scopeNote}`;
    renderCategoryChip(data);
    grid.innerHTML = data.items.map(p => productCardHTML(p)).join("");
    wireProductCardInteractions(grid);
  } catch (e) {
    statusEl.textContent = "Something went wrong analyzing that photo.";
    grid.innerHTML = `<div class="state-msg">${e.message}. Try a different image.</div>`;
  }
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

function renderVisualSearchResultsPage() {
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
        <a href="/category.html?category=${encodeURIComponent(p.category)}">${catLabel}</a> ›
        <span>${p.name}</span>
      </nav>
      <div class="pdp">
        <div class="pdp-media"><img src="${p.image_url}" alt="${p.name}" /></div>
        <div class="pdp-info">
          <p class="pdp-brand">${p.brand || "Staples"}</p>
          <h1 class="pdp-name">${p.name}</h1>
          <div class="stars">${starString(p.rating)} <span class="count">${p.rating} · ${p.reviews} reviews</span></div>
          <div class="pdp-price-row">
            <span class="price">$${p.price.toFixed(2)}</span>
            <span class="list-price">$${p.list_price.toFixed(2)}</span>
            <span class="pdp-savings">Save ${p.savings_pct}%</span>
          </div>
          <p class="pdp-desc">${p.description || ""}</p>
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
      addToCart(qty);
      addBtn.textContent = "Added ✓";
      addBtn.classList.add("added");
      setTimeout(() => { addBtn.textContent = "Add to Cart"; addBtn.classList.remove("added"); }, 1200);
    });

    // "You may also like" — same category
    renderProductRail("related-rail", { category: p.category });
  } catch (e) {
    el.innerHTML = `<div class="state-msg">${e.message}. <a href="/">Back to home</a></div>`;
  }
}
