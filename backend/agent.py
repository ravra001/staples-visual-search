"""
Staples AI — tool-calling chat agent.

Runs in-process inside the same FastAPI app/Cloud Run service as
everything else (see main.py's /api/agent/chat and _do_agent_chat) — no
separate service, no new deployment. Deliberately narrow: eleven
LLM-callable tools, every one of them a thin wrapper over deterministic
Python/SQL that already exists elsewhere in this app -- Gemini decides
WHAT to call and narrates the result, it never computes prices, ranks
matches, or invents products itself.

  search_products       — thin wrapper the ORCHESTRATION loop (main.py)
                           points at the existing hybrid search
                           (_hybrid_search) so there's no duplicate
                           search logic anywhere.
  plan_office_setup     — real, deterministic Python arithmetic against
                           real catalog prices, NOT something the model
                           computes itself. Same principle this app
                           already applies to price-intent parsing
                           (_parsePriceIntent in app.js): the model
                           extracts intent, plain code does the math.
                           Scoped narrowly to N-desks + N-chairs for a
                           headcount -- NOT a general room planner.
  plan_room_setup        — the general case plan_office_setup deliberately
                           isn't: one item per relevant category for a
                           whole room (living room, bedroom, etc), with an
                           optional style/color term, within a budget.
                           Same deterministic-math philosophy; style
                           matching is a plain substring filter over
                           catalog text, not a model judgment call.
  shop_the_room          — when a photo is attached to the chat AND it's a
                           room/space, run the EXISTING deterministic CV
                           pipeline (_do_shop_the_room in main.py: tile the
                           photo, embed, classify, vector-search per tile)
                           against it. Gemini's vision narrates what it
                           sees; it never invents the matches itself --
                           the real embedding search does, same as the
                           standalone Shop the Room feature.
  match_shopping_list    — resolves MULTIPLE distinct items to real catalog
                           products in one call, from either of two inputs:
                           a typed message ("5 staplers, 3 grey chairs, a
                           desk lamp") or a photo of a list/note/receipt --
                           the dispatch logic (main.py) doesn't care which,
                           it only ever reads args["items"] off the
                           function call. For the photo case specifically,
                           this is the OPPOSITE division of labor from
                           shop_the_room: reading messy real-world text off
                           an image is exactly what Gemini's vision is good
                           at and there's no deterministic OCR pipeline
                           elsewhere in this app to reuse, so Gemini reads
                           it and extracts {query, quantity} pairs itself;
                           resolving each one to a real catalog SKU is
                           then handed back to the same deterministic
                           search (_hybrid_search) everything else uses.
  not_shoppable           — the escape hatch for the two photo tools above. An
                           attached image is forced into calling ONE of
                           shop_the_room/match_shopping_list/not_shoppable
                           (see main.py's _do_agent_chat) so "attach a
                           photo, nothing happens" can't recur -- but
                           forcing a choice between only the first two
                           meant an irrelevant photo (a pet, a screenshot
                           of something unrelated) still got tiled and
                           vector-searched, returning confidently wrong
                           furniture instead of declining. This tool lets
                           the model opt out honestly instead.
  complete_the_look       — one stored product's own vector -> best match
                           per OTHER category (not the source item's own),
                           reusing shop_the_room's per-category search
                           machinery. Same underlying lookup as the
                           standalone product-page rail, exposed to chat
                           for "what goes with this desk" style requests.
  swap_bundle_item        — "make the desk cheaper" / "a nicer chair" for
                           any item already known from this conversation
                           (identified by sku, resolved via the existing
                           screen-context mechanism -- no separate bundle-
                           tracking state needed). Finds a genuinely better
                           cheaper/nicer alternative in the SAME category,
                           same deterministic-math philosophy as the
                           bundling tools. Works on any item shown, not
                           just ones from a bundle. Also handles an already-
                           picked specific replacement (new_sku, e.g. from a
                           find_similar result) and, when the item being
                           replaced was one of several in a confirmed order,
                           reconstructing the WHOLE updated order (other_skus)
                           so replacing one item doesn't silently drop the
                           rest -- see this function's own docstring.
  find_similar            — "like that one but in black" / "the same rug
                           but under $150" for a known sku. Reuses the exact
                           same _do_similar_search machinery as the
                           standalone /similar endpoint, so refine_text and
                           max_price behave identically everywhere.
  find_deals               — real list_price-vs-price discounts, optionally
                           scoped to a category and/or a minimum discount
                           percent. Plain sort over the catalog, not a model
                           guess at what's "on sale."
  compare_products         — pulls real price/rating/review data for 2-4
                           named skus so the model writes a tradeoff
                           comparison grounded in actual numbers instead of
                           paraphrasing from memory (see the system
                           prompt's compare_products carve-out: it's the
                           one tool allowed to state real numbers back).

Cart-adding is NOT a tool the model calls directly. A single searched
product already has a working Add-to-cart button on its product card (see
productCardHTML in app.js) — a redundant chat-driven single-SKU add path
adds nothing. The cases that ARE genuinely new -- adding a whole
plan_office_setup/plan_room_setup bundle, or every matched line of a
shopping list, in one action -- are plain frontend buttons keyed off the
response shape, not something the model decides to call.
"""
import re
import sys
import threading
import time
import traceback

import config
from products_data import get_product_by_sku, get_products_by_category, get_products_by_skus, get_deals, get_categories

_agent_state = {}
_agent_load_lock = threading.Lock()


def _get_agent_model():
    """Lazy-load the Gemini model once per process, mirroring
    embeddings.py's _get_clip() pattern exactly: a lock + double-check, not
    just a bare `if _agent_state: return` -- without it, two concurrent
    first-requests (plausible under Cloud Run's --concurrency=20) could
    both see an empty _agent_state and both start initializing, and the
    second one's _agent_state.update() partially overwriting the first's
    mid-flight is a real race, not a hypothetical one (same reasoning
    embeddings.py's docstring gives for CLIP's version of this lock).

    Retries the Vertex init + model construction once on failure -- a
    freshly cold-started Cloud Run instance's first Vertex auth handshake
    can transiently fail. Verified live: an otherwise-normal request
    degraded to the plain-search fallback (11s, wrong/generic results)
    on the first hit after the app had been idle, then an identical
    request succeeded in under 3s a minute later. Without a same-request
    retry, whichever user's request happens to land on a cold instance
    eats that failure -- often literally the first visitor. A genuinely
    persistent failure still exhausts the retry and correctly falls
    through to the honest degraded response in main.py."""
    if _agent_state:
        return _agent_state
    with _agent_load_lock:
        if _agent_state:  # someone else finished loading while we waited for the lock
            return _agent_state
        return _load_agent_model()


def _load_agent_model():
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, Tool
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "Staples AI requires google-cloud-aiplatform. Install it with: "
            "pip install -r requirements-ml.txt"
        ) from e

    project = config.GCP_PROJECT
    location = config.GCP_LOCATION
    if not project:
        raise RuntimeError("Staples AI requires embedding.vertex.project in config.yaml (or GCP_PROJECT) "
                            "-- same project used for Vertex embeddings, just a different Vertex API.")

    tool = Tool(function_declarations=[
        _search_products_decl(), _plan_office_setup_decl(), _plan_room_setup_decl(),
        _shop_the_room_decl(), _match_shopping_list_decl(), _complete_the_look_decl(),
        _not_shoppable_decl(), _swap_bundle_item_decl(),
        _find_similar_decl(), _find_deals_decl(), _compare_products_decl(),
    ])
    system_instruction = (
            "You are Staples AI, a shopping assistant for an office-supply and furniture catalog. "
            "Use search_products to find products by keyword or description for a SINGLE item. When "
            "the user asks for MULTIPLE distinct items in one message -- typed directly, e.g. '5 "
            "staplers, 3 grey chairs, a desk lamp' -- use match_shopping_list instead, with one "
            "{query, quantity} pair per item (quantity defaulting to 1 if none given): it gives "
            "quantity-aware results and a bulk add-to-cart action that separate search_products calls "
            "don't. Use plan_office_setup "
            "ONLY when the user gives a headcount of people and a budget to furnish desks+chairs for "
            "them (an office). Use plan_room_setup for any other whole-room or whole-space furnishing "
            "request (living room, bedroom, dining room, entryway, etc.) with a budget, optionally "
            "with a style or color -- do not tell the user you can't plan a room; that's what this "
            "tool is for. If a plan_room_setup result has an item with style_matched: false, say so "
            "honestly (e.g. 'no grey option for the rug, so I picked the best-rated one instead') "
            "rather than presenting it as if it matched. "
            "When a photo is attached: if it shows a room or physical space the user wants furnished, "
            "call shop_the_room (it takes no arguments -- the photo is already available server-side) "
            "and then narrate the real results it returns, don't invent your own guesses about what "
            "matches. If it shows a list, note, receipt, or screenshot of items the user wants to buy "
            "(including a photographed receipt or invoice you're reading to help the user reorder), "
            "read the text yourself and call match_shopping_list with the items you found as "
            "{query, quantity} pairs -- one entry per distinct item, quantity defaulting to 1 if none "
            "is written. For each query, use a CLEAN, GENERIC product term, not the literal receipt/"
            "packaging text -- e.g. a line reading 'COPY PAPER CASE  QTY 2' should become "
            "{query: 'copy paper', quantity: 2}, not {query: 'copy paper case', quantity: 2}; strip "
            "words like 'case', 'box', 'pack', 'HD', model numbers, and ALL-CAPS receipt formatting, "
            "they hurt matching against real catalog product names and a verbatim extraction has been "
            "verified live to fail on items ('copy paper case' finds nothing close; 'copy paper' finds "
            "the real product) that would otherwise match fine. If it's neither a room nor a list of "
            "items to shop for (a pet, an unrelated "
            "screenshot, anything not shoppable against this catalog), call not_shoppable with a short "
            "reason instead of guessing which of the other two to force it into. If a tool call reports "
            "no photo was attached, ask the user to attach one rather than guessing. "
            "Use complete_the_look when the user references a specific product (by name/sku from "
            "context or a prior search result) and asks what else goes with it, e.g. 'what else do I "
            "need for this desk?' -- search_products first if you don't already have its sku. "
            "Use find_deals when the user asks about discounts/sales, e.g. 'what's on sale in "
            "lighting?' or 'anything over 30% off?'. If its result has requested_category_not_found, "
            "say so honestly (e.g. \"no wall art deals right now, but here's what's on sale "
            "catalog-wide\") rather than presenting catalog-wide results as if they matched the "
            "category asked for. "
            "Keep replies to 2-3 sentences. Never state a specific price or SKU yourself -- the "
            "product cards already shown to the user have that; just refer to items by name. The ONE "
            "exception is compare_products: when narrating its result, DO state the real prices/"
            "ratings/review counts it returned, since comparing them numerically is the whole point of "
            "that tool. "
            "Some user messages will start with a bracketed [Context: ...] block listing what's "
            "currently shown on screen -- use it silently to resolve references like 'the second one' "
            "(e.g. by calling search_products with a refined query), but never quote or describe that "
            "block back to the user; it isn't something they wrote. When the user pushes back on ONE "
            "specific item already shown -- 'make the desk cheaper', 'a nicer chair', 'that but "
            "better' -- use swap_bundle_item with its sku from context and direction 'cheaper' or "
            "'nicer', instead of a fresh search_products call; it finds a real alternative in the same "
            "category rather than a possibly-unrelated new search result. If instead the user describes "
            "a STYLE/COLOR change or gives an explicit price cap for one known item -- 'the same rug "
            "but in blue', 'like that chair but under $100' -- use find_similar (sku + refine_text and/"
            "or max_price) instead; swap_bundle_item only moves along price/quality, it can't match a "
            "style or color change. This STILL applies even when the user doesn't explicitly say 'the "
            "same X' -- e.g. 'everything is fine except the monitor stand, looking for a tall monitor "
            "stand' is refining the monitor stand ALREADY shown (call find_similar with its sku from "
            "context and refine_text 'tall'), not a request for a brand-new, unrelated search; if you "
            "call search_products instead here, you'll search the whole catalog with no connection to "
            "the item being replaced and can return completely unrelated results (verified: this failure "
            "returned tables/desks for a monitor-stand request). "
            "Once the user has SEEN find_similar's results and picks one -- 'use that one instead', "
            "'the third one', 'replace it with the Jahnke desk' -- call swap_bundle_item again, but with "
            "new_sku set to the one they picked (not direction). If the item being replaced was part of "
            "a multi-item order the user already confirmed (a receipt reorder, a shopping list result) "
            "and they want everything ELSE kept as-is -- e.g. 'everything is fine except the monitor "
            "stand' -- also pass other_skus with the OTHER items' skus from context, so the result is "
            "one complete updated order instead of losing the rest. Do NOT tell the user their whole "
            "cart/order was replaced when they only asked to swap one item out of several -- that's "
            "exactly the failure other_skus exists to prevent. "
            "Use compare_products when the user asks "
            "to compare 2-4 specific known products, e.g. 'which of these is better' -- pass their skus "
            "from context."
    )

    last_err = None
    for attempt in range(2):
        try:
            vertexai.init(project=project, location=location)
            model = GenerativeModel(config.AGENT_MODEL, tools=[tool], system_instruction=system_instruction)
            _agent_state.update(model=model, Tool=Tool)
            return _agent_state
        except Exception as e:
            last_err = e
            print(f"[agent] Vertex model init failed (attempt {attempt + 1}/2):\n{traceback.format_exc()}",
                  file=sys.stderr)
            if attempt == 0:
                time.sleep(0.75)
    raise last_err


def _search_products_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="search_products",
        description="Search the product catalog by keyword or natural-language description.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, e.g. 'stapler' or 'something to print with'"},
            },
            "required": ["query"],
        },
    )


def _plan_office_setup_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="plan_office_setup",
        description="Plan a bulk desk+chair office setup for a given headcount and total budget in USD.",
        parameters={
            "type": "object",
            "properties": {
                "people_count": {"type": "integer", "description": "Number of people to furnish desks and chairs for"},
                "budget": {"type": "number", "description": "Total budget in US dollars for the whole setup"},
            },
            "required": ["people_count", "budget"],
        },
    )


def _plan_room_setup_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="plan_room_setup",
        description=(
            "Plan a whole-room furniture setup (living room, bedroom, dining room, entryway, etc.) "
            "within a total budget in USD, picking one item per relevant category. Optionally match "
            "a style or color, e.g. 'grey' or 'mid-century modern'. Use this for any full-room "
            "request that ISN'T specifically an office desk+chair headcount (that's plan_office_setup)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "room_type": {"type": "string", "description": "e.g. 'living room', 'bedroom', 'dining room', 'entryway', 'home office'"},
                "budget": {"type": "number", "description": "Total budget in US dollars for the whole room"},
                "style": {"type": "string", "description": "Optional style or color term, e.g. 'grey' or 'rustic'. Leave empty if none given."},
            },
            "required": ["room_type", "budget"],
        },
    )


def _complete_the_look_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="complete_the_look",
        description=(
            "Given one product's SKU (from context or a prior search result), find one coordinating "
            "item from each OTHER category to complete the look/room -- e.g. 'I just bought this desk, "
            "what else do I need?' Requires a real sku already known from this conversation; if none "
            "is available, search_products first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "The product SKU to find coordinating items for"},
            },
            "required": ["sku"],
        },
    )


def _shop_the_room_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="shop_the_room",
        description=(
            "Run the room-furnishing product matcher against the photo already attached to this "
            "conversation. Only call this when a photo is attached and it shows a room or space to "
            "furnish, not a list of items. Takes no arguments."
        ),
        parameters={"type": "object", "properties": {}},
    )


def _match_shopping_list_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="match_shopping_list",
        description=(
            "Resolve MULTIPLE distinct items to real catalog products in one call -- whether the user "
            "typed them directly in a message (e.g. '5 staplers, 3 grey chairs, a desk lamp') or you "
            "read them yourself off an attached photo of a list, note, or receipt. Gives quantity-aware "
            "results and a bulk add-to-cart action, unlike separate search_products calls. Pass every "
            "distinct item found. For a single item, use search_products instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "One entry per distinct item on the list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "What to search the catalog for, e.g. 'stapler' or 'copy paper'"},
                            "quantity": {"type": "integer", "description": "How many were requested; 1 if not specified"},
                        },
                        "required": ["query"],
                    },
                },
            },
            "required": ["items"],
        },
    )


def _not_shoppable_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="not_shoppable",
        description=(
            "Call this when a photo is attached but it's neither a room/space to furnish nor a list "
            "of items to shop for -- e.g. a pet, a person, an unrelated screenshot. Lets you decline "
            "honestly instead of being forced to guess between shop_the_room and match_shopping_list."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "One short phrase describing what the photo actually shows"},
            },
        },
    )


def _swap_bundle_item_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="swap_bundle_item",
        description=(
            "Find a genuinely cheaper or nicer alternative to one specific product already known from "
            "this conversation (from context or a prior result), in the SAME category. Use when the "
            "user pushes back on one item -- 'make the desk cheaper', 'a nicer chair'. ALSO use this "
            "(with new_sku set instead of direction) when the user has already picked a SPECIFIC "
            "replacement -- e.g. after seeing find_similar results and saying 'use that one instead'. "
            "If the item being replaced was part of a multi-item order (a receipt reorder, a shopping "
            "list) and the user wants to keep the other items, pass their skus as other_skus so the "
            "result is one complete updated bundle instead of losing them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "The SKU of the item to replace"},
                "direction": {"type": "string", "enum": ["cheaper", "nicer"], "description": "Whether to find a cheaper or a higher-quality alternative. Omit if new_sku is given."},
                "new_sku": {"type": "string", "description": "An exact replacement SKU the user has already picked (e.g. from a find_similar result), instead of a generic cheaper/nicer direction."},
                "other_skus": {
                    "type": "array", "items": {"type": "string"},
                    "description": "SKUs of the OTHER items from the same multi-item order that should stay unchanged, from context -- pass these when 'sku' was one item in a larger confirmed order and the user wants everything else kept as-is.",
                },
            },
            "required": ["sku"],
        },
    )


def _find_similar_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="find_similar",
        description=(
            "Find visually similar alternatives to one specific product already known from this "
            "conversation (by sku), optionally refined by style/color text and/or a max price -- "
            "e.g. 'like that one but warmer' or 'the same rug but under $150'. For plain 'cheaper'/"
            "'nicer' pushback with no style change, use swap_bundle_item instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "The SKU of the product to find alternatives to"},
                "refine_text": {"type": "string", "description": "Optional style/color refinement, e.g. 'in black' or 'more modern'. Leave empty if none given."},
                "max_price": {"type": "number", "description": "Optional maximum price. Leave unset if no budget given."},
            },
            "required": ["sku"],
        },
    )


def _find_deals_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="find_deals",
        description=(
            "Find the biggest real discounts (list price vs current price) in the catalog, "
            "optionally scoped to one category and/or a minimum discount percentage -- e.g. "
            "'what's on sale in lighting?' or 'anything over 30% off?'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Optional category to scope to, e.g. 'lighting'. Leave empty to search the whole catalog."},
                "min_discount_pct": {"type": "number", "description": "Optional minimum discount percentage, e.g. 30 for '30% off or more'."},
            },
        },
    )


def _compare_products_decl():
    from vertexai.generative_models import FunctionDeclaration
    return FunctionDeclaration(
        name="compare_products",
        description=(
            "Pull real price/rating/review data for 2-4 specific products (by sku, from context or a "
            "prior result) so you can write a genuine tradeoff comparison between them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "skus": {
                    "type": "array",
                    "description": "2 to 4 product SKUs to compare",
                    "items": {"type": "string"},
                },
            },
            "required": ["skus"],
        },
    )


def trim_items_for_model(items, cap=8):
    """Full _serialize()'d product dicts include description, image_url,
    list_price, savings_pct -- thousands of wasted tokens per turn feeding
    that back to the model. It only needs enough to talk about the items;
    the FULL objects still go to the browser separately for card rendering.
    Includes rating/reviews -- compare_products specifically needs real
    numbers to compare on (see its system-prompt carve-out: it's the one
    tool allowed to state them), and every other tool narrating "highly
    rated" benefits from actually knowing the number too."""
    out = []
    for p in items[:cap]:
        d = {"sku": p["sku"], "name": p["name"], "brand": p.get("brand", ""),
             "price": p["price"], "category": p["category"],
             "rating": p.get("rating"), "reviews": p.get("reviews")}
        if "requested_qty" in p:   # set by match_shopping_list -- worth the model knowing
            d["requested_qty"] = p["requested_qty"]
        if "style_matched" in p:   # set by plan_room_setup -- worth the model knowing
            d["style_matched"] = p["style_matched"]
        out.append(d)
    return out


_BUNDLE_ITEM_KEYS = ("desk", "chair", "shared_item", "closest_desk", "closest_chair")


def trim_bundle_for_model(bundle):
    """Same reasoning as trim_items_for_model, applied to plan_office_setup's
    output -- the raw bundle nests full product dicts (description, image_url)
    under desk/chair/shared_item/etc, and that was going straight into the
    model's tool-response context untrimmed: wasted tokens, and un-curated
    third-party catalog text (scraped ABO descriptions) landing in the
    model's context is a free-to-close prompt-injection surface even at
    this low-stakes demo scale."""
    if not bundle:
        return bundle
    out = {k: v for k, v in bundle.items() if k not in _BUNDLE_ITEM_KEYS}
    for key in _BUNDLE_ITEM_KEYS:
        item = bundle.get(key)
        if item:
            out[key] = trim_items_for_model([item], cap=1)[0]
    return out


def trim_room_bundle_for_model(bundle):
    """Same reasoning as trim_bundle_for_model, for plan_room_setup's shape
    (a flat `items` list instead of named desk/chair/shared_item keys)."""
    if not bundle:
        return bundle
    out = {k: v for k, v in bundle.items() if k != "items"}
    if bundle.get("items"):
        out["items"] = trim_items_for_model(bundle["items"], cap=12)
    return out


def trim_swap_for_model(result):
    """Same reasoning again, for swap_bundle_item's old_item/new_item pair
    (and its optional reconstructed bundle, when other_skus was given)."""
    if not result:
        return result
    out = {k: v for k, v in result.items() if k not in ("old_item", "new_item", "bundle")}
    for key in ("old_item", "new_item"):
        item = result.get(key)
        if item:
            out[key] = trim_items_for_model([item], cap=1)[0]
    if result.get("bundle"):
        out["bundle"] = trim_room_bundle_for_model(result["bundle"])
    return out


def _quality_score(p):
    """Bayesian-shrunk rating, not a bare rating -- the catalog's rating
    values sit on a coarse grid (mostly one decimal place), so ties are
    constant, and a bare `rating` reliably prefers a 5.0 with 3 reviews over
    a 4.6 with 3,000. Shrinks toward 0 as review count -> 0 so a fluke
    perfect score on a barely-reviewed item can't win."""
    rating = p.get("rating") or 0
    reviews = p.get("reviews") or 0
    return rating * reviews / (reviews + 20)


def _best_affordable(categories, budget):
    """Spend the given budget on genuine quality, not just the highest price
    that fits. An earlier version picked by price alone, which could return
    a 0-review $900 shelf as the "quality upgrade" purely because it was the
    most expensive thing under budget -- contradicts the quality-first
    philosophy every other pick in this module uses. Ranks by quality_score
    first, price second (so among near-equal quality, still prefer using
    more of the leftover budget rather than leaving it needlessly unspent).
    Tries each category in order, returning the best affordable item from
    the first category that has one at all."""
    for cat in categories:
        affordable = [p for p in get_products_by_category(cat, order_by_price=True) if p["price"] <= budget]
        if affordable:
            return max(affordable, key=lambda p: (_quality_score(p), p["price"]))
    return None


_NON_OFFICE_CHAIR_TERMS = (
    "barstool", "bar stool", "stool", "dining chair", "accent chair",
    "kids", "child", "teen", "toddler", "nursery", "rocking chair",
    "recliner", "chaise", "glider", "outdoor", "patio", "garden",
    "camping", "beach", "ottoman", "bench", "sofa", "loveseat", "floor chair",
)


def _office_chairs_only(chairs):
    """The catalog's single "chairs" category (fixed taxonomy, never
    subdivided) lumps genuine task/office chairs together with barstools,
    dining chairs, recliners, accent chairs, etc. Verified live:
    plan_office_setup picked a barstool -- rated 4.9 with 4,792 reviews,
    so high-quality that swap_bundle_item's "nicer" direction correctly
    found nothing better in the SAME category and honestly said so; the
    real bug was the initial pick, not the swap logic. A plain name-text
    exclusion (verified against the real catalog: 430 of 1086 "chairs"
    remain, cheapest at $48.99 -- barely above the $32.99 unfiltered
    cheapest, which turned out to be a recliner) is enough to fix it
    without touching the category taxonomy itself. Falls back to the
    unfiltered list only in the (currently unreachable, but cheap to
    guard) case that filtering leaves nothing at all."""
    filtered = [c for c in chairs if not any(t in c["name"].lower() for t in _NON_OFFICE_CHAIR_TERMS)]
    return filtered or chairs


def plan_office_setup(people_count, budget):
    """Real arithmetic against real catalog prices -- see module docstring
    for why this is deterministic Python, not something the LLM computes.

    Strategy: find the cheapest desk+chair combo; if even that doesn't fit
    the budget, say so honestly (report the closest achievable total
    rather than silently going over). If it fits with room to spare,
    filter each category down to what's actually affordable per person
    FIRST, then rank those survivors by quality -- ranking the 20 cheapest
    items by rating (the previous approach) meant the "upgrade" pool could
    never contain anything a bigger budget should unlock, so a $10,000
    budget for 4 people got the exact same furniture as a $900 one. Any
    leftover budget goes toward one shared item (storage or lighting).
    """
    try:
        people_count = max(1, min(int(people_count), 500))
        budget = max(1.0, float(budget))
    except (TypeError, ValueError):
        return {"feasible": False, "reason": "Could not understand the headcount or budget given."}

    # The 10k ABO catalog uses plural category names ("desks"/"chairs"); the
    # small built-in demo catalog (products_data.py, used when catalog_file
    # is unset) uses singular ("desk"/"chair") -- without this fallback,
    # running this tool -- the FIRST hero chip on the homepage -- against
    # the demo catalog always answered "no desks or chairs available."
    desks = get_products_by_category("desks", order_by_price=True) or \
        get_products_by_category("desk", order_by_price=True)
    chairs = _office_chairs_only(get_products_by_category("chairs", order_by_price=True) or
                                  get_products_by_category("chair", order_by_price=True))
    if not desks or not chairs:
        return {"feasible": False, "reason": "The catalog has no desks or chairs available right now."}

    cheapest_pair_total = people_count * (desks[0]["price"] + chairs[0]["price"])
    if cheapest_pair_total > budget:
        return {
            "feasible": False,
            "people_count": people_count, "budget": round(budget, 2),
            "closest_desk": desks[0], "closest_chair": chairs[0],
            "closest_total": round(cheapest_pair_total, 2),
        }

    # Small catalog (max ~1200 products/category), so a capped nested scan
    # over the top-20-by-quality survivors is fine -- no need for a real
    # optimizer here.
    per_person_budget = budget / people_count
    afford_desks = [d for d in desks if d["price"] <= per_person_budget]
    afford_chairs = [c for c in chairs if c["price"] <= per_person_budget]
    ranked_desks = sorted(afford_desks, key=_quality_score, reverse=True)[:20]
    ranked_chairs = sorted(afford_chairs, key=_quality_score, reverse=True)[:20]

    best = None
    for d in ranked_desks:
        for c in ranked_chairs:
            pair_price = d["price"] + c["price"]
            if pair_price <= per_person_budget:
                score = _quality_score(d) + _quality_score(c)
                if best is None or score > best[0] or (score == best[0] and pair_price < best[1]):
                    best = (score, pair_price, d, c)
    if best is None:
        # Defensive only: desks[0]/chairs[0] already proved individually
        # affordable by the cheapest_pair_total check above, so this
        # shouldn't be reachable -- but a live demo crashing on a float
        # rounding edge case is worse than a slightly non-optimal fallback.
        best = (0, desks[0]["price"] + chairs[0]["price"], desks[0], chairs[0])
    _, _, desk, chair = best
    furniture_total = people_count * (desk["price"] + chair["price"])

    remaining = budget - furniture_total
    shared_item = _best_affordable(("storage", "lighting"), remaining)

    total = furniture_total + (shared_item["price"] if shared_item else 0)
    return {
        "feasible": True,
        "people_count": people_count, "budget": round(budget, 2),
        "desk": desk, "chair": chair, "shared_item": shared_item,
        "total": round(total, 2), "under_by": round(budget - total, 2),
    }


# Maps a free-text room description to the catalog's fixed 13-category
# taxonomy (never renamed/restructured -- see fix_catalog_categories.py's
# self-consistency audit). Matched by substring against the user's room_type
# text, longest/most-specific key checked first so "home office" doesn't
# accidentally match a bare "office" entry that isn't there, and a plain
# "office" request routes here (not to plan_office_setup, which needs a
# headcount) for e.g. a single-person home-office room rather than N desks.
_ROOM_TYPE_CATEGORIES = {
    "living room": ["sofas", "tables", "rugs", "lighting", "wall_art"],
    "home office": ["desks", "chairs", "storage", "lighting"],
    "dining room": ["tables", "chairs", "wall_art", "lighting"],
    "bedroom": ["furniture", "storage", "lighting", "rugs"],
    "entryway": ["storage", "rugs", "wall_art", "lighting"],
    "kitchen": ["kitchen", "storage", "lighting"],
    "office": ["desks", "chairs", "storage", "lighting"],
}
_DEFAULT_ROOM_CATEGORIES = ["furniture", "home_decor", "lighting", "rugs"]


def _categories_for_room(room_type):
    key = (room_type or "").strip().lower()
    for name in sorted(_ROOM_TYPE_CATEGORIES, key=len, reverse=True):
        if name in key:
            return _ROOM_TYPE_CATEGORIES[name]
    return _DEFAULT_ROOM_CATEGORIES


_STYLE_STOPWORDS = {"the", "and", "for", "with", "style", "color", "colour", "some", "any"}


def _style_match(item, style_terms):
    """ALL terms must appear, not any -- verified live: "neon green plaid"
    matched (and was reported as fully matched) on a plain "Hunter Green"
    loveseat and a "Sea Green" lamp, neither neon nor plaid, because any()
    let a single word ("green") stand in for the whole compound phrase.
    Requiring every term is a real filter for a compound style description
    instead of one word doing all the work."""
    if not style_terms:
        return True
    haystack = f"{item.get('name', '')} {item.get('description', '')}".lower()
    return all(t in haystack for t in style_terms)


def plan_room_setup(room_type, budget, style=""):
    """The general case plan_office_setup deliberately isn't: one item per
    relevant category for a whole room, not N-of-a-pair for a headcount.

    Style matching is a plain substring check against catalog name/
    description text, not a model judgment call -- e.g. "grey" matches any
    item whose real product text says "grey" or "gray". Falls back to the
    category's cheapest item when nothing style-matches, rather than
    leaving that category empty over a wording mismatch.

    Strategy: cheapest (style-matched if possible) item per category first;
    if that base set is over budget, drop the priciest category picks one
    at a time until it fits (an honest scope reduction, not silently going
    over); spend any leftover budget upgrading picks to better-quality
    (still style-matched, still affordable) alternatives, cheapest-quality
    pick first so the weakest link gets upgraded before a strong one gets
    gold-plated.
    """
    try:
        budget = max(1.0, float(budget))
    except (TypeError, ValueError):
        return {"feasible": False, "reason": "Could not understand the budget given."}

    categories = _categories_for_room(room_type)
    # len > 2 + stopword filter: with all() requiring EVERY term to appear,
    # an unfiltered filler word ("a", "in") would make almost every product
    # fail the match on that word alone (barely any product text contains
    # "a" as a whole token the way a naive substring check needs it to).
    style_terms = [t for t in re.split(r"[,\s]+", (style or "").strip().lower())
                   if len(t) > 2 and t not in _STYLE_STOPWORDS]

    # Fetched once per category, reused by both the initial pick loop below
    # AND the upgrade loop further down -- each was independently calling
    # get_products_by_category for the same category (sofas alone is 1,190
    # rows under the 10k catalog), doubling the DB round trips a single
    # plan_room_setup call makes for no reason; a whole-room plan touches
    # several categories, so this is the difference between ~4 and ~8
    # queries for a typical 4-category room.
    items_by_cat = {cat: get_products_by_category(cat, order_by_price=True) for cat in categories}

    picks = {}
    for cat in categories:
        items = items_by_cat[cat]
        if not items:
            continue
        matching = [p for p in items if _style_match(p, style_terms)]
        picked = dict((matching or items)[0])
        # False only means a real style/color term was given and nothing in
        # this category's real product text matched it -- silently falling
        # back to the cheapest unrelated item read as the tool ignoring the
        # request, so the reply can now say so instead of pretending.
        picked["style_matched"] = bool(matching)
        picks[cat] = picked

    if not picks:
        return {"feasible": False, "reason": f"The catalog has nothing available for a {room_type or 'room'} setup right now."}

    while picks and sum(p["price"] for p in picks.values()) > budget:
        worst_cat = max(picks, key=lambda c: picks[c]["price"])
        del picks[worst_cat]

    if not picks:
        cheapest_per_category = [items[0] for cat in categories if (items := items_by_cat[cat])]
        cheapest = min(cheapest_per_category, key=lambda p: p["price"], default=None)
        return {
            "feasible": False, "room_type": room_type, "budget": round(budget, 2),
            "reason": "Budget is too low even for the single cheapest item.",
            "closest_item": cheapest,
        }

    remaining = budget - sum(p["price"] for p in picks.values())
    for cat in sorted(picks, key=lambda c: _quality_score(picks[c])):
        items = items_by_cat[cat]
        matching = [p for p in items if _style_match(p, style_terms)]
        pool = matching or items
        current = picks[cat]
        headroom = remaining + current["price"]
        affordable = [p for p in pool if p["price"] <= headroom]
        if affordable:
            best = max(affordable, key=lambda p: (_quality_score(p), p["price"]))
            if best["sku"] != current["sku"]:
                remaining -= (best["price"] - current["price"])
                best = dict(best)
                best["style_matched"] = bool(matching)
                picks[cat] = best

    total = sum(p["price"] for p in picks.values())
    return {
        "feasible": True,
        "room_type": room_type, "style": style, "budget": round(budget, 2),
        "items": list(picks.values()),
        "total": round(total, 2), "under_by": round(budget - total, 2),
    }


def _normalize_direction(direction):
    """Gemini should emit exactly "cheaper"/"nicer" (an enum'd parameter),
    but a defensive normalize is cheap insurance against a near-miss like
    "less expensive" or "better quality"."""
    d = (direction or "").strip().lower()
    if any(w in d for w in ("cheap", "budget", "less", "lower", "afford")):
        return "cheaper"
    return "nicer"


def swap_bundle_item(sku, direction=None, new_sku=None, other_skus=None):
    """"Make the desk cheaper" / "a nicer chair" -- for any item already
    known from this conversation, not just ones from a bundle. Deliberately
    stateless: no bundle/session needs to be tracked server-side, since the
    sku alone (already in the model's context via the screen-awareness
    mechanism) is enough to look up the item's real category and price and
    search that category directly. "cheaper" means the best-quality option
    still priced below the current item, not just the literal cheapest --
    same "spend wisely, don't just minimize" philosophy as the bundling
    tools. "nicer" prefers a same-or-higher-priced genuine upgrade over a
    bare quality_score comparison -- an earlier version picked whatever had
    the highest quality_score among ALL better-scoring candidates, which
    for a $375 desk meant recommending a $20 desk as "nicer" purely because
    it had thousands more reviews at the same star rating. Only falls back
    to a cheaper-but-better item when nothing pricier is actually better.

    new_sku: bypasses the cheaper/nicer search entirely -- the user has
    already picked a SPECIFIC replacement (e.g. from a find_similar result)
    rather than asking for a generic direction. direction is ignored when
    this is given.

    other_skus: the OTHER items from an existing multi-item order that
    should stay unchanged (e.g. the rest of a receipt reorder bundle).
    When given, the result includes a full reconstructed "bundle" (other
    items + the replacement, with a real total) instead of just the single
    swapped item -- this is what lets "everything's fine except the
    monitor stand, replace it with X" produce one updated "Add all to
    cart" bundle instead of losing the other confirmed items. Verified
    bug this fixes: without this, the only way to act on a chosen
    replacement was "replace the whole cart," dropping every other
    already-confirmed item.
    """
    current = get_product_by_sku((sku or "").strip())
    if not current:
        return {"feasible": False, "reason": "That item isn't from this conversation -- search for it first."}
    category = current["category"]

    if new_sku:
        new_item = get_product_by_sku(str(new_sku).strip())
        if not new_item:
            return {"feasible": False, "reason": "That replacement isn't from this conversation -- search for it first.",
                    "old_item": current, "category": category}
        direction = None
    else:
        direction = _normalize_direction(direction)
        candidates = [p for p in get_products_by_category(category) if p["sku"] != current["sku"]]
        if not candidates:
            return {"feasible": False, "reason": f"No other {category} products available to compare.", "old_item": current}

        if direction == "cheaper":
            pool = [p for p in candidates if p["price"] < current["price"]]
            if not pool:
                return {"feasible": False, "reason": "This is already the cheapest option in its category.",
                        "old_item": current, "category": category}
            new_item = max(pool, key=_quality_score)
        else:
            current_score = _quality_score(current)
            better = [p for p in candidates if _quality_score(p) > current_score]
            if not better:
                return {"feasible": False, "reason": "This is already one of the best-rated options in its category.",
                        "old_item": current, "category": category}
            upgrades = [p for p in better if p["price"] >= current["price"]]
            # Cheapest genuine upgrade (same-or-higher price, better quality) if
            # one exists; otherwise the single best-quality item overall is the
            # honest answer even though it happens to cost less.
            new_item = min(upgrades, key=lambda p: p["price"]) if upgrades else max(better, key=_quality_score)

    result = {
        "feasible": True, "category": category, "direction": direction,
        "old_item": current, "new_item": new_item,
        "price_delta": round(new_item["price"] - current["price"], 2),
    }

    other_skus = [str(s).strip() for s in (other_skus or []) if s and str(s).strip() and str(s).strip() != current["sku"]]
    if other_skus:
        by_sku = get_products_by_skus(other_skus)
        # Silently drop any sku that doesn't resolve (stale/invalid context
        # reference) rather than failing the whole swap over it -- the
        # items that DO resolve still form a valid, honest bundle.
        other_items = [by_sku[s] for s in other_skus if s in by_sku]
        bundle_items = other_items + [new_item]
        result["bundle"] = {
            "feasible": True,
            "items": bundle_items,
            "total": round(sum(p["price"] for p in bundle_items), 2),
        }
    return result


def find_deals(category=None, min_discount_pct=None):
    """Real discount data (list_price vs price), not a model guess -- same
    deterministic-math philosophy as every other tool here. category is
    optional (catalog-wide if omitted).

    Ranks/filters via get_deals (sql: get_deals_page, one SQL query with the
    discount floor and LIMIT pushed down; memory: _mem_deals, the same
    sort/filter over PRODUCTS) rather than fetching the whole catalog into
    Python and slicing 8 rows out of it here -- get_products_page's
    sort="deals" already does exactly this ranking for the homepage rail;
    this used to duplicate that logic AND do it the slow way, dragging the
    full catalog (description text included) over the wire on every
    catalog-wide "what's on sale?" chat turn."""
    requested_category = (category or "").strip() or None
    category = (requested_category or "").lower().replace(" ", "_").replace("-", "_") or None
    unmatched_category = None
    if category is not None:
        # Gemini passes a category the way the user said it ("wall art"),
        # not the catalog's underscored slug ("wall_art") -- the replace()
        # above handles that. If it STILL isn't a real category (a plural/
        # singular mismatch, a category that doesn't exist in this catalog),
        # fall back to catalog-wide rather than confidently declining "no
        # products found" when a category filter typo is the actual issue --
        # but keep the ORIGINAL request visible in the response so the
        # narration can say "no wall art deals, but here's what's on sale
        # catalog-wide" instead of silently dropping the user's intent.
        if category not in set(get_categories()):
            unmatched_category = requested_category
            category = None
    try:
        min_discount_pct = float(min_discount_pct) if min_discount_pct is not None else None
    except (TypeError, ValueError):
        min_discount_pct = None

    top = get_deals(category, min_discount_pct, limit=8)
    if not top:
        if min_discount_pct is not None:
            return {"feasible": False, "category": category, "min_discount_pct": min_discount_pct,
                    "reason": "Nothing meets that discount threshold right now."}
        return {"feasible": False, "reason": f"No products found{f' in {category!r}' if category else ''}."}
    result = {"feasible": True, "category": category, "min_discount_pct": min_discount_pct, "items": top}
    if unmatched_category:
        result["requested_category_not_found"] = unmatched_category
    return result


def compare_products(skus):
    """Real product data for 2-4 known skus, not model-recalled numbers --
    same principle as every other tool here: the model decides WHAT to
    compare, plain lookups supply the real prices/ratings/reviews it
    compares them ON (see the system prompt's compare_products carve-out
    -- the one tool allowed to state real numbers back to the user)."""
    skus = [str(s).strip() for s in (skus or []) if s and str(s).strip()][:4]
    found, missing = [], []
    for sku in skus:
        p = get_product_by_sku(sku)
        if p:
            found.append(p)
        else:
            missing.append(sku)
    return {"feasible": len(found) >= 2, "items": found, "missing": missing}
