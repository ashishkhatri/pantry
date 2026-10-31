"""The recipe feed: recipes from every source, matched against what's in the house.

Sources (all stored in the ``recipes`` table):
  * mealdb  TheMealDB, a free library of ~300 recipes, downloaded automatically.
  * link    any recipe page you paste in; read from the schema.org Recipe data that
            nearly every recipe site publishes. Keeps the link to the original.
  * web     Spoonacular recipe search by what you have (optional free API key).
  * ai      written by your AI around what's in stock, favouring what expires soon.

The feed is sorted by how many ingredients you'd still need: everything on hand first.
A background refresh (see ``needs_refresh``) adds web and AI recipes when the pantry
has changed or the feed is old, so the page always has something to cook.

Matching ingredient names to product names is fuzzy: "chicken breast" matches
"Boneless Skinless Chicken Breasts", "olive oil" matches "Extra Virgin Olive Oil".
Staples (Settings) such as salt and water are always assumed to be there.
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache
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
    "cup", "tablespoon", "tbsp", "teaspoon", "tsp", "pound", "ounce", "gram", "kilogram", "litre", "liter",
    "pinch", "dash", "handful", "about", "plus", "more", "divided", "into", "cut", "inch", "thick", "thin",
    "room", "temperature", "needed", "as", "if", "desired", "all", "purpose", "with", "without", "your",
    "favorite", "favourite", "good", "quality", "store", "bought", "homemade", "can", "heaping", "level",
}
# A product with one of these words is a different food from the plain ingredient:
# "peanut butter" isn't "butter", "coconut milk" isn't "milk".
IDENTITY = {"peanut", "almond", "coconut", "soy", "oat", "ice", "sour", "cashew", "hazelnut", "sweet", "condensed",
            "evaporated", "powder", "powdered", "sauce", "paste", "juice", "vinegar", "extract", "stock", "broth",
            "cream", "seasoning", "flavored", "flavor", "chips", "bar", "bars", "soup", "mix", "dressing",
            "bell", "chili", "chile", "cayenne", "jalapeno", "poblano", "serrano", "habanero", "flake"}
# British (TheMealDB) and alternative names -> one spelling.
SYNONYMS = {"stock": "broth", "aubergine": "eggplant", "courgette": "zucchini", "coriander": "cilantro",
            "prawn": "shrimp", "garbanzo": "chickpea", "capsicum": "pepper", "rocket": "arugula",
            "scallion": "spring", "yoghurt": "yogurt", "mince": "ground", "beetroot": "beet", "chilli": "chili"}
# Parts of a food that still count as that food: "chicken thighs" are chicken.
CUTS = {"breast", "thigh", "wing", "drumstick", "leg", "fillet", "steak", "chop", "loin", "tenderloin", "rib",
        "shank", "clove", "bulb", "head", "stalk", "sprig", "bunch", "floret", "kernel", "piece", "slice", "strip",
        "wedge", "half", "whole", "skin", "ground", "patty", "link", "cube", "chunk"}
# Food nouns: a word that's itself the main word of some ingredient ("tortilla", "broth", "sauce").
# The recipes in the database add hundreds more (see food_heads()).
FOOD_NOUNS = {"tortilla", "broth", "stock", "sauce", "paste", "bread", "bun", "roll", "cookie", "cracker", "chip",
              "cake", "pie", "muffin", "bar", "cereal", "pasta", "noodle", "rice", "soup", "milk", "cream", "cheese",
              "butter", "oil", "vinegar", "juice", "jam", "jelly", "syrup", "powder", "flour", "sugar", "salt",
              "seasoning", "dressing", "dip", "spread", "yogurt", "egg", "bean", "pea", "nut", "seed", "candy",
              "chocolate", "tea", "coffee", "water", "soda", "wine", "beer", "pizza", "burger", "sausage", "bacon",
              "ham", "salami", "taco", "wrap", "pita", "bagel", "waffle", "pancake", "biscuit", "pretzel", "popcorn",
              "puff", "stick", "nugget", "tender", "jerky", "mix", "batter", "dough", "crust", "filling", "frosting",
              "icing", "extract", "oatmeal", "granola", "smoothie", "shake", "gravy", "salsa", "hummus", "pesto",
              "ketchup", "mustard", "mayonnaise", "mayo", "relish", "pickle", "olive", "sandwich", "lasagna", "ravioli"}
_heads_cache = {"n": -1, "set": frozenset(FOOD_NOUNS)}


def food_heads() -> frozenset:
    return _heads_cache["set"]


def learn_food_heads(con) -> None:
    """Add the main word of every recipe ingredient we know about to the food-noun list."""
    n = con.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    if n == _heads_cache["n"]:
        return
    heads = set(FOOD_NOUNS)
    for (ings,) in con.execute("SELECT ingredients FROM recipes"):
        for i in json.loads(ings or "[]"):
            t = tokens(i.get("name") or "")
            if t and len(t) <= 3:        # short names only: "flour tortillas", not "1 cup of ..." lines
                heads.add(t[-1])
    heads -= CUTS | OPTIONAL_HEADS | NOISE
    _heads_cache.update(n=n, set=frozenset(heads))


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


@lru_cache(maxsize=20000)
def _tokens(name: str) -> tuple:
    return tuple(tokens_uncached(name))


def tokens(name: str) -> List[str]:
    return list(_tokens(name or ""))


def tokens_uncached(name: str) -> List[str]:
    t = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    t = t.split(",")[0] if len(t) > 25 else t
    t = t.translate(_PUNCT)
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
    # "cheddar cheese" is "cheddar": drop an optional trailing head word when something is left.
    if a[-1] != b[-1]:
        a = a[:-1] if len(a) > 1 and a[-1] in OPTIONAL_HEADS else a
        b = b[:-1] if len(b) > 1 and b[-1] in OPTIONAL_HEADS else b
    sa, sb = set(a), set(b)
    if sa <= sb:
        extra = sb - sa
        # "butter" vs "peanut butter": an identity word on only one side means a different food.
        if extra & IDENTITY:
            return False
        # "butter" vs "butter tortillas", "chicken" vs "chicken broth": another food word after the
        # matched one is the real food, so it's something else. Descriptions ("whole milk vitamin d")
        # and cuts of meat ("chicken breasts" for "chicken") are fine.
        last = max(i for i, w in enumerate(b) if w in sa)
        return not any(w in food_heads() and w not in CUTS for w in b[last + 1:])
    if sb <= sa:
        extra = sa - sb
        if extra <= OPTIONAL_HEADS:
            return True
        # Product "oil" covers ingredient "olive oil"; product "cream" doesn't cover "sour cream".
        return not (extra & IDENTITY) and len(sb) >= 1 and a[-1] == b[-1]
    return False


def find_match(ingredient: str, pantry: List[dict]) -> Optional[dict]:
    for p in pantry:
        if matches(ingredient, p.get("ingredient_name") or p["name"]):
            return p
    return None


def is_staple(ingredient: str, staples: Iterable[str]) -> bool:
    """"Kosher salt" and "freshly ground black pepper" are covered by the staples salt and pepper;
    "red bell pepper" isn't."""
    return any(matches(ingredient, st) for st in staples if tokens(st))


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


# ---------------- storage ----------------

def _row_out(r) -> dict:
    d = dict(r)
    d["ingredients"] = json.loads(r["ingredients"] or "[]")
    d["steps"] = json.loads(r["steps"]) if r["steps"] else None
    return d


def store(con, rec: dict) -> Optional[int]:
    """Insert or update one recipe. Deduplicates by source id, or by title within a source."""
    ings = [{"name": str(i.get("name") or "").strip()[:120], "amount": str(i.get("amount") or "").strip()[:60]}
            for i in rec.get("ingredients") or [] if str(i.get("name") or "").strip()]
    if not rec.get("title") or not ings:
        return None
    vals = dict(source=rec["source"], ext_id=rec.get("ext_id"), title=str(rec["title"]).strip()[:200],
                summary=(rec.get("summary") or None), image=rec.get("image"), url=rec.get("url"),
                site=rec.get("site") or (urllib.parse.urlsplit(rec["url"]).hostname or "").removeprefix("www.")
                if rec.get("url") else None,
                category=rec.get("category"), area=rec.get("area"), time_minutes=rec.get("time_minutes"),
                servings=rec.get("servings"), ingredients=json.dumps(ings), instructions=rec.get("instructions"),
                steps=json.dumps(rec["steps"]) if rec.get("steps") else None)
    if vals["ext_id"]:
        row = con.execute("SELECT id FROM recipes WHERE source = ? AND ext_id = ?", (vals["source"], vals["ext_id"])).fetchone()
    else:
        row = con.execute("SELECT id FROM recipes WHERE source = ? AND lower(title) = lower(?)",
                          (vals["source"], vals["title"])).fetchone()
    cols = list(vals)
    if row:
        con.execute(f"UPDATE recipes SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                    [vals[c] for c in cols] + [row[0]])
        return row[0]
    return con.execute(f"INSERT INTO recipes({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [vals[c] for c in cols]).lastrowid


def get(con, recipe_id: int) -> Optional[dict]:
    r = con.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not r:
        return None
    d = _row_out(r)
    learn_food_heads(con)
    d.update(check_ingredients(d["ingredients"], inventory.pantry_names(con), staples(con)))
    return d


def counts(con) -> Dict[str, int]:
    return dict(con.execute("SELECT source, COUNT(*) FROM recipes WHERE hidden = 0 GROUP BY source").fetchall())


# ---------------- the feed ----------------

def feed(con, q: str = "", source: str = "", can_make: bool = False, offset: int = 0, limit: int = 24,
         max_missing: int = 6) -> dict:
    """Recipes worth showing, sorted: nothing missing first, then by how few ingredients you'd need.
    Ties go to recipes that use things expiring soon, then to ones that use more of your food."""
    pantry = inventory.pantry_names(con)
    st = staples(con)
    learn_food_heads(con)
    sql, args = "SELECT * FROM recipes WHERE hidden = 0", []
    if source:
        sql += " AND source = ?"
        args.append(source)
    if q.strip():
        like = f"%{q.strip()}%"
        sql += " AND (title LIKE ? OR category LIKE ? OR area LIKE ? OR ingredients LIKE ? OR site LIKE ?)"
        args += [like] * 5
    out = []
    for r in con.execute(sql, args).fetchall():
        d = _row_out(r)
        chk = check_ingredients(d["ingredients"], pantry, st)
        used = [h for h in chk["have"] if not h.get("staple")]
        # Library and web recipes must use something you have; your links and AI recipes always show.
        if d["source"] in ("mealdb", "web") and not used:
            continue
        if len(chk["missing"]) > (0 if can_make else max_missing) and d["source"] not in ("link",):
            continue
        d.update(chk)
        d.pop("instructions", None)
        d.pop("steps", None)
        out.append(d)
    # Fewest missing first; then ones using things that expire soon; then ones using more of your food;
    # then the newest (fresh web and AI finds float up among equals).
    out.sort(key=lambda x: (len(x["missing"]), -len(x["uses_soon"]), -len(x["have"]), -x["id"]))
    total = len(out)
    return {"recipes": out[offset:offset + limit], "total": total, "offset": offset}


# ---------------- refreshing ----------------

def pantry_signature(con) -> str:
    ids = [str(p["id"]) for p in inventory.pantry_names(con)]
    return hashlib.sha1(",".join(ids).encode()).hexdigest()[:16] + f":{len(ids)}"


def needs_refresh(con, hours: int = 12) -> bool:
    """Refresh when the feed is old, or the set of things in stock changed noticeably."""
    if dbm.get_setting(con, "recipes_auto") != "1":
        return False
    last = dbm.get_setting(con, "last_feed_refresh") or ""
    if not last:
        return True
    try:
        if datetime.now() - datetime.fromisoformat(last) > timedelta(hours=hours):
            return True
    except ValueError:
        return True
    old = dbm.get_setting(con, "feed_signature") or ""
    new = pantry_signature(con)
    if old.split(":")[0] == new.split(":")[0]:
        return False
    # Different stock: refresh if it changed by 3+ products (or the pantry was nearly empty before)
    try:
        before, now = int(old.split(":")[1]), int(new.split(":")[1])
    except (IndexError, ValueError):
        return True
    return abs(now - before) >= 3 or before < 3 or datetime.now() - datetime.fromisoformat(last) > timedelta(hours=2)


def mark_refreshed(con) -> None:
    dbm.set_setting(con, "last_feed_refresh", datetime.now().isoformat(timespec="seconds"))
    dbm.set_setting(con, "feed_signature", pantry_signature(con))
    con.commit()


def prune_ai(con, keep: int = 60) -> None:
    """Keep the newest AI recipes; older ones are replaced by fresher ideas."""
    con.execute("DELETE FROM recipes WHERE source = 'ai' AND id NOT IN "
                "(SELECT id FROM recipes WHERE source = 'ai' ORDER BY id DESC LIMIT ?)", (keep,))


def focus_ingredients(con, n: int = 12) -> List[str]:
    """What to search with: expiring things first, then the rest of the fresh food."""
    p = inventory.pantry_names(con)
    p.sort(key=lambda x: (x.get("status") not in ("soon", "expired"), x.get("first_exp") or "9999"))
    names, seen = [], set()
    for it in p:
        key = " ".join(tokens(it["name"])[-2:])
        if key and key not in seen:
            seen.add(key)
            names.append(key)
        if len(names) >= n:
            break
    return names


# ---------------- TheMealDB ----------------

def fetch_mealdb(con, fetch=None) -> dict:
    """Download every TheMealDB recipe (a-z). ``fetch`` is for tests."""
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
            store(con, {"source": "mealdb", "ext_id": m["idMeal"], "title": m["strMeal"], "category": m.get("strCategory"),
                        "area": m.get("strArea"), "image": m.get("strMealThumb"),
                        "url": m.get("strSource") or m.get("strYoutube") or None,
                        "instructions": m.get("strInstructions"), "ingredients": ings})
            n += 1
    con.commit()
    return {"recipes": n}


def _get_json(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "PantryApp", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LookupError(f"{urllib.parse.urlsplit(url).hostname} answered {e.code}.") from e
    except (OSError, ValueError) as e:
        raise LookupError(f"Couldn't download from {urllib.parse.urlsplit(url).hostname} ({e}).") from e


def mealdb_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM recipes WHERE source = 'mealdb'").fetchone()[0]


# ---------------- Spoonacular (web search) ----------------

SPOON = "https://api.spoonacular.com"


def fetch_spoonacular(con, key: str, ingredients: List[str], number: int = 12, fetch=None) -> int:
    """Recipes from around the web that use what you have (ranked to need the fewest extras)."""
    fetch = fetch or _get_json
    if not ingredients:
        return 0
    found = fetch(f"{SPOON}/recipes/findByIngredients?" + urllib.parse.urlencode(
        {"ingredients": ",".join(ingredients), "number": number, "ranking": 2, "ignorePantry": "true", "apiKey": key}))
    ids = [str(r["id"]) for r in found or [] if r.get("id")]
    if not ids:
        return 0
    info = fetch(f"{SPOON}/recipes/informationBulk?" + urllib.parse.urlencode({"ids": ",".join(ids), "apiKey": key}))
    n = 0
    for r in info or []:
        steps = [s["step"] for block in r.get("analyzedInstructions") or [] for s in block.get("steps") or [] if s.get("step")]
        summary = re.sub(r"<[^>]+>", "", r.get("summary") or "")
        summary = re.split(r"(?<=[.!?])\s", summary)[0][:300] if summary else None
        if store(con, {"source": "web", "ext_id": str(r["id"]), "title": r.get("title"), "image": r.get("image"),
                       "url": r.get("sourceUrl") or r.get("spoonacularSourceUrl"), "summary": summary,
                       "time_minutes": r.get("readyInMinutes"), "servings": r.get("servings"),
                       "category": ", ".join((r.get("dishTypes") or [])[:1]) or None,
                       "area": ", ".join((r.get("cuisines") or [])[:1]) or None,
                       "ingredients": [{"name": i.get("nameClean") or i.get("name"), "amount": i.get("original") or ""}
                                       for i in r.get("extendedIngredients") or []],
                       "steps": steps or None}):
            n += 1
    con.commit()
    return n


# ---------------- import from a link ----------------

def _iso_minutes(v) -> Optional[int]:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(v or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def _first(v):
    if isinstance(v, list):
        return _first(v[0]) if v else None
    if isinstance(v, dict):
        return v.get("url") or v.get("@id")
    return v


def _steps(v) -> List[str]:
    out = []
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"\n+|(?<=\.)\s{2,}", htmlmod.unescape(re.sub(r"<[^>]+>", "\n", v))) if s.strip()]
    for x in v or []:
        if isinstance(x, str):
            out.append(x.strip())
        elif isinstance(x, dict):
            if x.get("itemListElement"):
                out += _steps(x["itemListElement"])
            elif x.get("text"):
                out.append(htmlmod.unescape(re.sub(r"<[^>]+>", "", x["text"])).strip())
    return [s for s in out if s]


def _find_recipe(node):
    if isinstance(node, list):
        for x in node:
            r = _find_recipe(x)
            if r:
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
            return node
        for k in ("@graph", "mainEntity", "itemListElement"):
            if k in node:
                r = _find_recipe(node[k])
                if r:
                    return r
    return None


def parse_recipe_page(page: str, url: str) -> dict:
    """The schema.org Recipe on a page (JSON-LD), as a recipe dict."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        r = _find_recipe(data)
        if not r:
            continue
        ings = [htmlmod.unescape(str(x)).strip() for x in r.get("recipeIngredient") or r.get("ingredients") or []]
        y = r.get("recipeYield")
        y = y[0] if isinstance(y, list) and y else y
        servings = int(re.search(r"\d+", str(y)).group()) if y and re.search(r"\d+", str(y)) else None
        desc = htmlmod.unescape(re.sub(r"<[^>]+>", "", str(r.get("description") or ""))).strip()
        return {"source": "link", "title": htmlmod.unescape(str(r.get("name") or "")).strip(), "url": url,
                "image": _first(r.get("image")), "summary": desc[:300] or None,
                "time_minutes": _iso_minutes(r.get("totalTime")) or _iso_minutes(r.get("cookTime")),
                "servings": servings, "category": _first(r.get("recipeCategory")),
                "area": _first(r.get("recipeCuisine")),
                "ingredients": [{"name": i, "amount": ""} for i in ings], "steps": _steps(r.get("recipeInstructions")) or None}
    raise LookupError("Couldn't find a recipe on that page. Try the recipe's own page rather than a list or search page.")


def import_url(con, url: str, fetch_page=None) -> int:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise LookupError("Paste a full link starting with http:// or https://.")
    if fetch_page is None:
        def fetch_page(u):
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (compatible; PantryApp recipe import)",
                                                     "Accept": "text/html,application/xhtml+xml"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read(4_000_000).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                raise LookupError(f"That site answered {e.code}; it may block automated readers.") from e
            except OSError as e:
                raise LookupError(f"Couldn't open that link ({e}).") from e
    rec = parse_recipe_page(fetch_page(url), url)
    rid = store(con, rec)
    if not rid:
        raise LookupError("That page's recipe has no ingredient list.")
    con.commit()
    return rid


def web_search_url(title: str) -> str:
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": f"{title} recipe"})
