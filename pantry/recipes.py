"""Recipe ideas from what's in the house.

Two sources:
  * TheMealDB (free, ~300 recipes): downloaded once into the database, then matched
    against your stock locally, so it works without any AI.
  * The AI (when configured): writes recipes around what you have, favouring items
    that expire soon. Its "in the house" claims are re-checked here.

Matching ingredient names to product names is fuzzy: "chicken breast" matches
"Boneless Skinless Chicken Breasts", "olive oil" matches "Extra Virgin Olive Oil".
Staples (Settings) such as salt and water are always assumed to be there.
"""
from __future__ import annotations

import json
import re
import string
import urllib.request
from typing import Dict, Iterable, List, Optional, Set

from . import db as dbm
from . import inventory

MEALDB_URL = "https://www.themealdb.com/api/json/v1/1/search.php?f={letter}"

# Words that describe preparation or packaging, not the food itself.
NOISE = {
    "fresh", "freshly", "chopped", "diced", "sliced", "minced", "grated", "shredded", "large", "small", "medium",
    "whole", "organic", "can", "cans", "canned", "tin", "tins", "jar", "bag", "box", "pack", "package", "of", "and",
    "or", "the", "a", "an", "to", "taste", "for", "boneless", "skinless", "extra", "virgin", "lean", "low", "fat",
    "reduced", "sodium", "free", "unsalted", "salted", "plain", "natural", "raw", "frozen", "dried", "ground",
    "finely", "roughly", "thinly", "cooked", "uncooked", "ripe", "peeled", "pitted", "halved", "cubed", "softened",
    "melted", "beaten", "optional", "garnish", "pieces", "piece", "style", "brand", "family", "size", "value",
    "great", "kirkland", "signature", "select", "premium", "classic", "original", "oz", "lb", "lbs", "g", "kg",
    "ml", "l", "gal", "ct", "pk", "fl", "qt", "pt", "x",
}
# A product with one of these words is a different food from the plain ingredient:
# "peanut butter" isn't "butter", "coconut milk" isn't "milk".
IDENTITY = {"peanut", "almond", "coconut", "soy", "oat", "ice", "sour", "cashew", "hazelnut", "sweet", "condensed",
            "evaporated", "powder", "powdered", "sauce", "paste", "juice", "vinegar", "extract", "stock", "broth",
            "cream", "seasoning", "flavored", "flavor", "chips", "bar", "bars", "soup", "mix", "dressing"}
# British (TheMealDB) and alternative names -> one spelling.
SYNONYMS = {"stock": "broth", "aubergine": "eggplant", "courgette": "zucchini", "coriander": "cilantro",
            "prawn": "shrimp", "garbanzo": "chickpea", "capsicum": "pepper", "rocket": "arugula",
            "scallion": "spring", "yoghurt": "yogurt", "mince": "ground", "beetroot": "beet", "chilli": "chili"}
# Head words that can be left off: "cheddar" is "cheddar cheese".
OPTIONAL_HEADS = {"cheese", "fillet", "meat", "leaves", "leaf"}
_PUNCT = str.maketrans({c: " " for c in string.punctuation})


def singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("oes", "ches", "shes", "sses")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokens(name: str) -> List[str]:
    t = (name or "").lower().translate(_PUNCT)
    out = []
    for w in t.split():
        if any(ch.isdigit() for ch in w):
            continue
        w = SYNONYMS.get(singular(w), singular(w))
        if w and w not in NOISE:
            out.append(w)
    return out


def matches(ingredient: str, product: str) -> bool:
    """Does a pantry product satisfy a recipe ingredient?"""
    a, b = tokens(ingredient), tokens(product)
    if not a or not b:
        return False
    sa, sb = set(a), set(b)
    if sa <= sb:
        extra = sb - sa
        # "butter" vs "peanut butter": an identity word on only one side means a different food.
        return not (extra & IDENTITY)
    if sb <= sa:
        extra = sa - sb
        if extra <= OPTIONAL_HEADS:
            return True
        # Product "oil" covers ingredient "olive oil"; product "cream" doesn't cover "sour cream".
        return not (extra & IDENTITY) and len(sb) >= 1 and a[-1] == b[-1]
    return False


def find_match(ingredient: str, pantry: List[dict]) -> Optional[dict]:
    for p in pantry:
        if matches(ingredient, p["name"]):
            return p
    return None


def is_staple(ingredient: str, staples: Iterable[str]) -> bool:
    t = set(tokens(ingredient))
    return any(set(tokens(s)) and set(tokens(s)) == t for s in staples)


def staples(con) -> List[str]:
    raw = dbm.get_setting(con, "staples") or ""
    return [s.strip() for s in re.split(r"[\n,]", raw) if s.strip()]


def check_ingredients(ingredients: List[dict], pantry: List[dict], staple_list: List[str]) -> dict:
    have, missing, uses_soon = [], [], []
    for ing in ingredients:
        name = ing["name"]
        if is_staple(name, staple_list):
            have.append({**ing, "staple": True})
            continue
        m = find_match(name, pantry)
        if m:
            have.append({**ing, "product": m["name"], "status": m.get("status")})
            if m.get("status") in ("soon", "expired") and m["name"] not in uses_soon:
                uses_soon.append(m["name"])
        else:
            missing.append(ing)
    return {"have": have, "missing": missing, "uses_soon": uses_soon}


# ---------------- TheMealDB ----------------

def fetch_mealdb(con, fetch=None) -> dict:
    """Download every TheMealDB recipe (a-z) into the database. ``fetch`` is for tests."""
    fetch = fetch or _get_json
    n = 0
    for letter in string.ascii_lowercase:
        data = fetch(MEALDB_URL.format(letter=letter)) or {}
        for m in data.get("meals") or []:
            ings = []
            for i in range(1, 21):
                name = (m.get(f"strIngredient{i}") or "").strip()
                if name:
                    ings.append({"name": name, "amount": (m.get(f"strMeasure{i}") or "").strip()})
            con.execute("INSERT OR REPLACE INTO mealdb(id, name, category, area, thumb, source_url, instructions, "
                        "ingredients, fetched_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                        (m["idMeal"], m["strMeal"], m.get("strCategory"), m.get("strArea"), m.get("strMealThumb"),
                         m.get("strSource") or m.get("strYoutube"), m.get("strInstructions"), json.dumps(ings)))
            n += 1
    con.commit()
    return {"recipes": n}


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "PantryApp"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise LookupError(f"Couldn't download recipes from TheMealDB ({e}).") from e


def mealdb_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM mealdb").fetchone()[0]


def from_mealdb(con, max_missing: int = 3, limit: int = 60, q: str = "") -> List[dict]:
    pantry = inventory.pantry_names(con)
    if not pantry:
        return []
    st = staples(con)
    sql, args = "SELECT * FROM mealdb", []
    if q.strip():
        sql += " WHERE name LIKE ? OR category LIKE ? OR area LIKE ?"
        args = [f"%{q.strip()}%"] * 3
    out = []
    for r in con.execute(sql, args).fetchall():
        ings = json.loads(r["ingredients"])
        chk = check_ingredients(ings, pantry, st)
        real_have = [h for h in chk["have"] if not h.get("staple")]
        if len(chk["missing"]) > max_missing or not real_have:
            continue
        out.append({"source": "mealdb", "id": r["id"], "title": r["name"], "category": r["category"],
                    "area": r["area"], "thumb": r["thumb"], "url": r["source_url"],
                    "instructions": r["instructions"], "ingredients": ings, **chk})
    out.sort(key=lambda x: (len(x["missing"]), -len(x["uses_soon"]), -len(x["have"]), x["title"]))
    return out[:limit]


def check_ai_recipes(con, recipes: List[dict]) -> List[dict]:
    pantry = inventory.pantry_names(con)
    st = staples(con)
    out = []
    for r in recipes:
        chk = check_ingredients(r["ingredients"], pantry, st)
        out.append({"source": "ai", **r, **chk})
    out.sort(key=lambda x: (len(x["missing"]), -len(x["uses_soon"])))
    return out
