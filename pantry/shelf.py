"""Built-in shelf-life table: food categories, how long each keeps where, and
keyword rules for guessing a category from a product name.

Numbers are conservative "quality" estimates loosely based on the USDA
FoodKeeper guidance. They're starting points: the AI (when configured) or you
can override them per product, and you can always set a date on an item.

Days are ``None`` where that storage doesn't make sense (e.g. raw chicken in the
pantry). ``opened`` is how long it keeps once opened, in its usual place.
Edit this file freely; it's plain data.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# key: (label, pantry, fridge, freezer, opened, default location)
CATEGORIES: Dict[str, Tuple[str, Optional[int], Optional[int], Optional[int], Optional[int], str]] = {
    "milk":           ("Milk & cream",          None, 7,    90,   5,    "fridge"),
    "plant_milk":     ("Plant milk",            270,  10,   None, 7,    "fridge"),   # pantry days = shelf-stable cartons
    "cheese_hard":    ("Hard cheese",           None, 120,  180,  30,   "fridge"),
    "cheese_soft":    ("Soft cheese",           None, 14,   90,   7,    "fridge"),
    "yogurt":         ("Yogurt & sour cream",   None, 14,   60,   7,    "fridge"),
    "butter":         ("Butter",                None, 60,   270,  30,   "fridge"),
    "eggs":           ("Eggs",                  None, 35,   None, None, "fridge"),
    "meat_raw":       ("Raw meat",              None, 4,    180,  None, "fridge"),
    "ground_meat":    ("Ground meat",           None, 2,    120,  None, "fridge"),
    "poultry_raw":    ("Raw poultry",           None, 2,    270,  None, "fridge"),
    "seafood_raw":    ("Raw fish & seafood",    None, 2,    180,  None, "fridge"),
    "deli":           ("Deli & cured meat",     None, 14,   60,   5,    "fridge"),
    "leftovers":      ("Leftovers & cooked",    None, 4,    90,   None, "fridge"),
    "bread":          ("Bread & baked goods",   5,    14,   90,   None, "pantry"),
    "tortillas":      ("Tortillas & wraps",     7,    30,   180,  None, "pantry"),
    "fruit":          ("Fresh fruit",           5,    14,   270,  None, "fridge"),
    "fruit_counter":  ("Bananas, avocados etc.", 5,   7,    90,   None, "pantry"),
    "berries":        ("Berries",               None, 5,    270,  None, "fridge"),
    "vegetables":     ("Fresh vegetables",      None, 10,   270,  None, "fridge"),
    "leafy_greens":   ("Leafy greens & herbs",  None, 5,    None, 3,    "fridge"),
    "root_veg":       ("Potatoes, onions etc.", 30,   60,   None, None, "pantry"),
    "canned":         ("Canned goods",          730,  None, None, 4,    "pantry"),
    "dry_goods":      ("Pasta, rice, grains",   730,  None, None, 365,  "pantry"),
    "beans_dry":      ("Dried beans & lentils", 730,  None, None, 365,  "pantry"),
    "flour_baking":   ("Flour & baking mixes",  240,  365,  730,  180,  "pantry"),
    "sugar":          ("Sugar & sweeteners",    730,  None, None, 730,  "pantry"),
    "spices":         ("Spices & seasonings",   1095, None, None, 1095, "pantry"),
    "oil_vinegar":    ("Oil & vinegar",         365,  None, None, 180,  "pantry"),
    "condiments":     ("Condiments & sauces",   365,  180,  None, 60,   "pantry"),
    "jarred_sauce":   ("Pasta sauce & salsa",   365,  None, None, 7,    "pantry"),
    "nut_butter":     ("Nut butter & spreads",  270,  None, None, 90,   "pantry"),
    "cereal":         ("Cereal & oats",         270,  None, None, 90,   "pantry"),
    "snacks":         ("Snacks, chips, crackers", 120, None, None, 14,  "pantry"),
    "nuts":           ("Nuts & seeds",          180,  365,  730,  90,   "pantry"),
    "dried_fruit":    ("Dried fruit",           180,  365,  None, 90,   "pantry"),
    "coffee_tea":     ("Coffee & tea",          365,  None, None, 30,   "pantry"),
    "beverages":      ("Drinks",                270,  270,  None, 5,    "pantry"),
    "juice":          ("Juice (refrigerated)",  None, 10,   240,  7,    "fridge"),
    "frozen":         ("Frozen foods",          None, None, 240,  None, "freezer"),
    "ice_cream":      ("Ice cream",             None, None, 60,   None, "freezer"),
    "soup_broth":     ("Broth & stock",         365,  None, None, 4,    "pantry"),
    "tofu":           ("Tofu & plant protein",  None, 30,   150,  4,    "fridge"),
    "baby":           ("Baby food & formula",   365,  None, None, 2,    "pantry"),
    "other":          ("Other",                 180,  14,   180,  14,   "pantry"),
}

# Where these go once opened, when that's not where they're kept unopened.
OPENED_FRIDGE = {"plant_milk", "canned", "jarred_sauce", "condiments", "soup_broth", "baby", "juice", "beverages",
                 "tortillas"}


def opened_location_for_category(cat: str) -> str:
    loc = CATEGORIES.get(cat, CATEGORIES["other"])[5]
    return "fridge" if loc == "pantry" and cat in OPENED_FRIDGE else loc


# Keyword rules, checked in order; first match wins. Word-boundary matched on a
# lower-cased name, so put specific phrases before generic words.
KEYWORDS: List[Tuple[str, str]] = [
    (r"ice cream|gelato|popsicle|frozen yogurt", "ice_cream"),
    (r"\bfrozen\b|\bfz\b|pizza rolls|tater tots|hash browns", "frozen"),
    (r"peanut butter|almond butter|nutella|hazelnut spread|\bjam\b|jelly|preserves", "nut_butter"),
    (r"almond milk|oat milk|soy milk|coconut milk beverage|rice milk", "plant_milk"),
    (r"sour cream|cottage cheese|cream cheese", "yogurt"),
    (r"\byogh?urt|kefir", "yogurt"),
    (r"parmesan|cheddar|swiss|gouda|pecorino|romano|asiago|colby|monterey|provolone", "cheese_hard"),
    (r"mozzarella|brie|feta|ricotta|goat cheese|queso|burrata|camembert|cheese", "cheese_soft"),
    (r"\bmilk\b|half and half|half & half|heavy cream|whipping cream|creamer|buttermilk", "milk"),
    (r"\bbutter\b|margarine", "butter"),
    (r"\beggs?\b", "eggs"),
    (r"ground (beef|turkey|pork|chicken|lamb)|hamburger|sausage", "ground_meat"),
    (r"chicken|turkey|duck\b", "poultry_raw"),
    (r"salmon|tuna steak|shrimp|cod\b|tilapia|fish|scallop|crab|lobster", "seafood_raw"),
    (r"\bham\b|salami|pepperoni|prosciutto|deli|bacon|hot dog|bologna|lunch meat", "deli"),
    (r"steak|beef|pork|lamb|veal|ribs|roast", "meat_raw"),
    (r"leftover|cooked|rotisserie|prepared meal", "leftovers"),
    (r"tofu|tempeh|seitan", "tofu"),
    (r"\bcanned\b|\bcan\b|\btin\b|tuna|beans? in|chickpeas|garbanzo|kidney beans|black beans|pinto|refried|"
     r"diced tomatoes|tomato paste|tomato sauce|crushed tomatoes|corn kernels|spam|evaporated|condensed", "canned"),
    (r"broth|stock\b|bouillon|soup", "soup_broth"),
    (r"pasta sauce|marinara|salsa|alfredo|pesto", "jarred_sauce"),
    (r"ketchup|mustard|mayo|mayonnaise|hot sauce|sriracha|soy sauce|bbq|barbecue|dressing|relish|worcestershire|"
     r"teriyaki|fish sauce|hoisin|sauce", "condiments"),
    (r"olive oil|vegetable oil|canola|\boil\b|vinegar|cooking spray", "oil_vinegar"),
    (r"flour|baking soda|baking powder|cornstarch|yeast|cake mix|brownie mix|pancake mix|cornmeal|cocoa", "flour_baking"),
    (r"sugar|honey|maple syrup|syrup|molasses|stevia|sweetener", "sugar"),
    (r"\bsalt\b|pepper(corns)?\b|cumin|paprika|oregano|basil leaves|cinnamon|spice|seasoning|chili powder|"
     r"garlic powder|onion powder|thyme|rosemary|nutmeg|turmeric|curry|vanilla", "spices"),
    (r"lentils|dried beans|split peas|dry beans", "beans_dry"),
    (r"pasta|spaghetti|penne|macaroni|noodles|ramen|rice\b|quinoa|couscous|barley|farro|orzo|lasagna", "dry_goods"),
    (r"cereal|oats|oatmeal|granola|muesli|cheerios|flakes", "cereal"),
    (r"chips|crackers|pretzels|popcorn|cookies|candy|chocolate|granola bar|snack|goldfish|puffs", "snacks"),
    (r"almonds|walnuts|pecans|cashews|peanuts|pistachios|seeds|\bnuts\b", "nuts"),
    (r"raisins|dried (fruit|cranberries|apricots|mango)|prunes|dates\b", "dried_fruit"),
    (r"coffee|espresso|\btea\b|matcha", "coffee_tea"),
    (r"orange juice|apple juice|juice", "juice"),
    (r"soda|cola|sparkling|seltzer|water\b|sports drink|gatorade|beer|wine|kombucha|energy drink|lemonade", "beverages"),
    (r"tortilla|wraps?\b|pita|naan", "tortillas"),
    (r"bread|bagel|bun|rolls?\b|muffin|croissant|english muffin|baguette|cake|donut", "bread"),
    (r"strawberr|blueberr|raspberr|blackberr|berries|cherries|grapes", "berries"),
    (r"banana|avocado|tomato(es)?\b|peach|plum|mango|pear|kiwi|nectarine", "fruit_counter"),
    (r"apple|orange|lemon|lime|grapefruit|melon|pineapple|clementine|mandarin|fruit", "fruit"),
    (r"lettuce|spinach|kale|arugula|cilantro|parsley|basil|mint|greens|salad|herbs|chard|cabbage", "leafy_greens"),
    (r"potato|onion|garlic|sweet potato|yam|shallot|squash|pumpkin", "root_veg"),
    (r"carrot|broccoli|cauliflower|celery|cucumber|zucchini|bell pepper|peppers|mushroom|asparagus|green beans|"
     r"corn\b|peas\b|eggplant|radish|beet|brussels|jalape|vegetable|veggie", "vegetables"),
    (r"formula|baby food", "baby"),
]
_COMPILED = [(re.compile(p), c) for p, c in KEYWORDS]

# Open Food Facts category tags -> our categories (checked most specific first).
OFF_TAGS: List[Tuple[str, str]] = [
    ("ice-creams", "ice_cream"), ("frozen-foods", "frozen"), ("plant-based-milks", "plant_milk"),
    ("nut-butters", "nut_butter"), ("spreads", "nut_butter"), ("jams", "nut_butter"),
    ("yogurts", "yogurt"), ("cream-cheeses", "yogurt"), ("hard-cheeses", "cheese_hard"), ("cheeses", "cheese_soft"),
    ("milks", "milk"), ("creams", "milk"), ("butters", "butter"), ("eggs", "eggs"),
    ("poultries", "poultry_raw"), ("fishes", "seafood_raw"), ("seafood", "seafood_raw"),
    ("hams", "deli"), ("sausages", "ground_meat"), ("meats", "meat_raw"), ("tofu", "tofu"),
    ("canned-foods", "canned"), ("canned-vegetables", "canned"), ("canned-fishes", "canned"),
    ("soups", "soup_broth"), ("broths", "soup_broth"),
    ("pasta-sauces", "jarred_sauce"), ("salsas", "jarred_sauce"), ("sauces", "condiments"), ("condiments", "condiments"),
    ("vegetable-oils", "oil_vinegar"), ("vinegars", "oil_vinegar"),
    ("flours", "flour_baking"), ("baking", "flour_baking"), ("sugars", "sugar"), ("honeys", "sugar"), ("syrups", "sugar"),
    ("spices", "spices"), ("condiments-and-spices", "spices"), ("salts", "spices"),
    ("breakfast-cereals", "cereal"), ("nuts", "nuts"), ("dried-fruits", "dried_fruit"),
    ("legumes", "beans_dry"), ("pastas", "dry_goods"), ("rices", "dry_goods"), ("cereals-and-their-products", "dry_goods"),
    ("chips", "snacks"), ("crisps", "snacks"), ("biscuits", "snacks"),
    ("crackers", "snacks"), ("confectioneries", "snacks"), ("chocolates", "snacks"), ("snacks", "snacks"),
    ("coffees", "coffee_tea"), ("teas", "coffee_tea"),
    ("fruit-juices", "juice"), ("juices", "juice"), ("beverages", "beverages"),
    ("breads", "bread"), ("pastries", "bread"), ("cakes", "bread"),
    ("fresh-fruits", "fruit"), ("fruits", "fruit"), ("leaf-vegetables", "leafy_greens"),
    ("fresh-vegetables", "vegetables"), ("vegetables", "vegetables"), ("baby-foods", "baby"),
]


def guess_category(name: str, off_tags: Iterable[str] = ()) -> str:
    """Category from Open Food Facts tags if any match, otherwise from keywords in the name."""
    tags = [t.split(":", 1)[-1] for t in off_tags or ()]
    for tag, cat in OFF_TAGS:
        if tag in tags:
            return cat
    n = (name or "").lower()
    for rx, cat in _COMPILED:
        if rx.search(n):
            return cat
    return "other"


def category_info(cat: str) -> dict:
    label, pantry, fridge, freezer, opened, loc = CATEGORIES.get(cat, CATEGORIES["other"])
    return {"key": cat if cat in CATEGORIES else "other", "label": label, "pantry_days": pantry,
            "fridge_days": fridge, "freezer_days": freezer, "opened_days": opened, "default_location": loc,
            "opened_location": opened_location_for_category(cat)}


def all_categories() -> List[dict]:
    return [category_info(k) for k in CATEGORIES]


def shelf_days(product, location: str) -> Optional[int]:
    """Days a product keeps in a location: its own override, else the category default.
    ``product`` is a mapping with category and shelf_*_days keys."""
    def raw(loc):
        own = product[f"shelf_{loc}_days"] if f"shelf_{loc}_days" in product.keys() else None
        return int(own) if own is not None else category_info(product["category"])[f"{loc}_days"]

    days = raw(location)
    if days is not None:
        return days
    # Not where this food is usually kept. Pick something sensible rather than no date.
    if location == "pantry":       # a perishable left out
        return 1
    if location == "fridge":       # shelf-stable in the fridge keeps at least as long; thawed frozen food ~3 days
        return raw("pantry") if raw("pantry") is not None else 3
    return raw("fridge") or raw("pantry")   # freezing never shortens it


def opened_location(product) -> str:
    """Where an item of this product goes once opened: its own setting, else the category's."""
    own = product["opened_location"] if "opened_location" in product.keys() else None
    if own:
        return own
    return opened_location_for_category(product["category"])


def opened_days(product) -> Optional[int]:
    own = product["opened_days"] if "opened_days" in product.keys() else None
    if own is not None:
        return int(own)
    return category_info(product["category"])["opened_days"]
