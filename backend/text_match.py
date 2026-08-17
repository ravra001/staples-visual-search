"""Small text-matching helper shared across every keyword-matching
implementation in this app (products_data.py's in-memory backend,
products_repo_sql.py's SQL backend, and main.py's post-hoc plausibility
check for match_shopping_list) -- kept in its own leaf module with no
app-specific imports so none of those introduce a circular import on
each other by importing it directly from one another.
"""


def singularize(word):
    """Cheap plural stripping, not a real stemmer -- "staplers" -> "stapler",
    "lamps" -> "lamp", "boxes" -> "box". Doesn't need to be linguistically
    complete, just needs to stop a plural query word from failing a
    singular product name's substring check -- verified live: "monitors"
    and "notebooks" ranked completely differently from "monitor"/
    "notebook" even though the catalog has real matches for both, because
    the keyword side of hybrid search requires an exact substring hit and
    no product is literally named "Monitors" (plural)."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "ches", "shes")) and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word
