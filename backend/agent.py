"""
Staples AI — tool-calling chat agent.

Runs in-process inside the same FastAPI app/Cloud Run service as
everything else (see main.py's /api/agent/chat and _do_agent_chat) — no
separate service, no new deployment. Deliberately narrow: exactly two
LLM-callable tools.

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

Cart-adding is NOT a tool. A single searched product already has a
working Add-to-cart button on its product card (see productCardHTML in
app.js) — exposing a redundant chat-driven single-SKU add path adds
nothing. The one case that IS genuinely new — adding a whole
plan_office_setup bundle in one action — is a plain frontend button
keyed off the presence of a `bundle` in the response, not something the
model decides to call.
"""
import config
from products_data import get_products_by_category

_agent_state = {}


def _get_agent_model():
    """Lazy-load the Gemini model once per process, mirroring
    embeddings.py's _get_vertex() pattern exactly (same project,
    same credentials, different Vertex API)."""
    if _agent_state:
        return _agent_state
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
    vertexai.init(project=project, location=location)
    tool = Tool(function_declarations=[_search_products_decl(), _plan_office_setup_decl()])
    model = GenerativeModel(
        config.AGENT_MODEL,
        tools=[tool],
        system_instruction=(
            "You are Staples AI, a shopping assistant for an office-supply and furniture catalog. "
            "Use search_products to find products by keyword or description. Use plan_office_setup "
            "when the user gives a headcount and a budget for furnishing an office. Keep replies to "
            "2-3 sentences. Never state a specific price or SKU yourself -- the product cards already "
            "shown to the user have that; just refer to items by name."
        ),
    )
    _agent_state.update(model=model, Tool=Tool)
    return _agent_state


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


def trim_items_for_model(items, cap=8):
    """Full _serialize()'d product dicts include description, image_url,
    list_price, savings_pct -- thousands of wasted tokens per turn feeding
    that back to the model. It only needs enough to talk about the items;
    the FULL objects still go to the browser separately for card rendering."""
    return [
        {"sku": p["sku"], "name": p["name"], "brand": p.get("brand", ""),
         "price": p["price"], "category": p["category"]}
        for p in items[:cap]
    ]


def plan_office_setup(people_count, budget):
    """Real arithmetic against real catalog prices -- see module docstring
    for why this is deterministic Python, not something the LLM computes.

    Strategy: find the cheapest desk+chair combo; if even that doesn't fit
    the budget, say so honestly (report the closest achievable total
    rather than silently going over). If it fits with room to spare, try
    to upgrade to a better-rated combo that still fits everyone, and use
    any leftover budget on one shared item (storage or lighting).
    """
    try:
        people_count = max(1, min(int(people_count), 500))
        budget = max(1.0, float(budget))
    except (TypeError, ValueError):
        return {"feasible": False, "reason": "Could not understand the headcount or budget given."}

    desks = _cheapest_first(get_products_by_category("desks"))
    chairs = _cheapest_first(get_products_by_category("chairs"))
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

    # Room to spare -- look for a better-rated combo that still fits
    # everyone, not just the bare-minimum cheapest option. Small catalog
    # (max ~1200 products/category), so a capped nested scan is fine --
    # no need for a real optimizer here.
    per_person_budget = budget / people_count
    best = None
    for d in desks[:20]:
        for c in chairs[:20]:
            pair_price = d["price"] + c["price"]
            if pair_price <= per_person_budget:
                score = (d.get("rating") or 0) + (c.get("rating") or 0)
                if best is None or score > best[0] or (score == best[0] and pair_price < best[1]):
                    best = (score, pair_price, d, c)
    _, _, desk, chair = best
    furniture_total = people_count * (desk["price"] + chair["price"])

    remaining = budget - furniture_total
    shared_item = None
    for cat in ("storage", "lighting"):
        for item in _cheapest_first(get_products_by_category(cat)):
            if item["price"] <= remaining:
                shared_item = item
                break
        if shared_item:
            break

    total = furniture_total + (shared_item["price"] if shared_item else 0)
    return {
        "feasible": True,
        "people_count": people_count, "budget": round(budget, 2),
        "desk": desk, "chair": chair, "shared_item": shared_item,
        "total": round(total, 2), "under_by": round(budget - total, 2),
    }


def _cheapest_first(products):
    return sorted(products, key=lambda p: p.get("price") or float("inf"))
